"""The system clipboard, and the `[ImgN]` placeholders that come out of it.

A terminal cannot hand a picture to the program running inside it. Bracketed
paste — the only paste a terminal knows — carries text, and Textual's own
`App.clipboard` is an internal buffer of things copied *inside* the app, not
the system one. So `ctrl+v` here is not a paste at all: it is dai walking out
to the operating system and asking what is on the clipboard, through the same
commands Claude Code uses (`osascript` on macOS, `xclip`/`wl-paste` on Linux).

Two halves live here. `has_image`/`save_image`/`read_text` are the platform
end, and they never raise: a missing binary, a non-zero exit and a timeout all
mean the same thing to a person pressing a key — nothing was on the clipboard —
and an argument between two agents is not worth losing over a clipboard.

`Attachments` is the bookkeeping: it writes the picture into the run's own
directory and hands back the `[ImgN]` token that stands in for it. The token is
what the user sees and edits; `resolve()` swaps it for the path at the moment
the text is sent, because a path in the box is what nobody asked for and a
token in the prompt is something no agent can open.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from dai.snapshot import ignore_locally, toplevel

#: A clipboard read that has not answered by now is not going to. The number is
#: generous for the work (one `osascript` is tens of milliseconds) and small
#: enough that a wedged helper costs a noticeable pause, not the session.
TIMEOUT = 2.0

#: `[Img1]`, `[Img12]`. Deliberately narrow: anything else the user typed by
#: hand is their text, and `resolve` leaves it alone.
TOKEN = re.compile(r"\[Img(\d+)\]")


# --- the platform end -----------------------------------------------------


async def _run(argv: list[str], *, capture: bool = False) -> tuple[int, bytes] | None:
    """Run a command to completion. `None` means it could not be run at all."""

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return None

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except (asyncio.TimeoutError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    return proc.returncode or 0, stdout or b""


def _applescript_path(dest: Path) -> str:
    literal = str(dest).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{literal}"'


async def has_image() -> bool:
    """Is there a picture on the clipboard right now?

    Asked separately from `save_image` on purpose: it is what keeps a `ctrl+v`
    over ordinary text from creating an empty `images/` directory that would
    then show up as a run that never happened.
    """

    if sys.platform == "darwin":
        # stdout is the image as a hex literal — megabytes of it — and we want
        # only the exit code, so it goes nowhere.
        result = await _run(["osascript", "-e", "the clipboard as «class PNGf»"])
        return result is not None and result[0] == 0

    if sys.platform.startswith("linux"):
        for argv in (
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            ["wl-paste", "-l"],
        ):
            result = await _run(argv, capture=True)
            if result is not None and result[0] == 0 and b"image/" in result[1]:
                return True
        return False

    return False


async def save_image(dest: Path) -> bool:
    """Write the clipboard's picture to `dest` as PNG."""

    if sys.platform == "darwin":
        # Order is load-bearing: the data is fetched *before* the file is
        # opened, so a clipboard holding no picture fails without leaving a
        # zero-byte file behind.
        result = await _run(
            [
                "osascript",
                "-e",
                "set png_data to (the clipboard as «class PNGf»)",
                "-e",
                f"set fp to open for access POSIX file {_applescript_path(dest)} "
                "with write permission",
                "-e",
                "write png_data to fp",
                "-e",
                "close access fp",
            ]
        )
        return result is not None and result[0] == 0 and dest.is_file()

    if sys.platform.startswith("linux"):
        for argv in (
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            ["wl-paste", "--type", "image/png"],
        ):
            result = await _run(argv, capture=True)
            if result is None or result[0] != 0 or not result[1]:
                continue
            try:
                dest.write_bytes(result[1])
            except OSError:
                return False
            return True
        return False

    return False


async def read_text() -> str | None:
    """Whatever text is on the clipboard, or `None` if there is none."""

    if sys.platform == "darwin":
        candidates = [["pbpaste"]]
    elif sys.platform.startswith("linux"):
        candidates = [
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
        ]
    else:
        return None

    for argv in candidates:
        result = await _run(argv, capture=True)
        if result is not None and result[0] == 0 and result[1]:
            return result[1].decode("utf-8", errors="replace")
    return None


# --- the bookkeeping ------------------------------------------------------


class Attachments:
    """The pictures pasted into one prompt, and what they are called."""

    def __init__(self, workspace: Path, images_dir: Path) -> None:
        self.workspace = Path(workspace)
        self.images_dir = Path(images_dir)
        self._paths: dict[int, str] = {}

    async def grab(self) -> str | None:
        """Take the clipboard's picture, if there is one. Returns its token."""

        if not await has_image() or not self._ensure():
            return None

        number = self._next_number()
        dest = self.images_dir / f"img{number}.png"
        if not await save_image(dest):
            # A token pointing at a half-written file is worse than no token:
            # the agent opens nothing and says so a round later.
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        self._paths[number] = self._reference(dest)
        return f"[Img{number}]"

    def resolve(self, text: str) -> str:
        """Swap every token we minted for the path it stands for."""

        def swap(match: re.Match[str]) -> str:
            return self._paths.get(int(match.group(1)), match.group(0))

        return TOKEN.sub(swap, text)

    # --- internals --------------------------------------------------------

    def _ensure(self) -> bool:
        """Make the directory, and hide it from git before anything is in it."""

        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Same bargain the transcript strikes: a workspace we cannot write
            # to costs the feature, not the run.
            return False
        # `__main__` does this too, later. Here is where it has to happen as
        # well, because a pasted picture is now the first thing dai writes —
        # earlier than the transcript, and earlier than the first snapshot.
        if (repo := toplevel(self.workspace)) is not None:
            ignore_locally(repo, ".dai/")
        return True

    def _next_number(self) -> int:
        """The lowest free `imgN.png`.

        Counted off the directory rather than a counter in memory: the startup
        prompt and every later inject screen each build their own
        `Attachments` over the same run directory, and two of them counting
        from one would write `img1.png` twice.
        """

        used = {n for n in (self._number_of(p) for p in self._existing()) if n}
        return max(used, default=0) + 1

    def _existing(self) -> list[Path]:
        try:
            return list(self.images_dir.glob("img*.png"))
        except OSError:
            return []

    @staticmethod
    def _number_of(path: Path) -> int | None:
        digits = path.stem[3:]
        return int(digits) if digits.isdigit() else None

    def _reference(self, dest: Path) -> str:
        """How the path is written into the prompt the agents receive.

        Relative to the workspace when it can be — that is what the engines are
        given as `cwd`, and it is the same shape `@` completion inserts.
        """

        try:
            return dest.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(dest)
