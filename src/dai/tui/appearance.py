"""Whether the terminal is light or dark, and when that changes.

Three questions, answered by three separate mechanisms:

*Before the app starts*, `detect()` asks the terminal outright with OSC 11 —
"what is your background colour?" — and decides by luminance. This has to
happen before the first frame or the user sees a flash of the wrong palette.
It is also the only part that works on every terminal.

*While the app runs*, `AppearanceDriver` turns on mode 2031, which asks the
terminal to say so whenever its colour scheme changes. Ghostty, kitty, WezTerm,
foot, Contour, recent iTerm2 and Windows Terminal answer; the rest simply never
report, and the theme stays as it was found. Nothing breaks either way.

*Whatever the terminal says* arrives on stdin as an escape sequence, which is
the awkward part. Textual's parser reissues sequences it does not recognise as
literal keypresses, so a scheme report — or a late answer to our own OSC 11
query, arriving after `detect()` gave up — would be typed into whatever has
focus. `SchemeParser` takes them out of the stream first. That is as much what
makes this safe as what makes it work.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from collections.abc import Iterable
from functools import lru_cache
from typing import Literal

from textual import constants
from textual._xterm_parser import XTermParser
from textual.message import Message

try:  # the tty dance is POSIX-only; elsewhere we simply never know
    import termios
    import tty
except ImportError:  # pragma: no cover - not reachable on a supported platform
    termios = tty = None  # type: ignore[assignment]

Appearance = Literal["dark", "light"]

#: "What is your background colour?" — answered as an OSC 11 with an XParseColor
#: value. Universally supported, but only answered once.
QUERY_BACKGROUND = "\x1b]11;?\x1b\\"

#: Primary device attributes. Every terminal answers this, and answers it in
#: order, so it makes a reliable full stop: once the DA1 reply is in, we know
#: no OSC 11 reply is still coming and can stop waiting.
QUERY_ATTRIBUTES = "\x1b[c"

#: Mode 2031: report colour-scheme changes from now on. The terminal answers
#: with a DSR immediately and again whenever the scheme changes.
ENABLE_SCHEME_REPORTS = "\x1b[?2031h"
DISABLE_SCHEME_REPORTS = "\x1b[?2031l"

#: "And what is it right now?" — the same DSR, on demand. Free self-correction
#: for the case where the pre-launch OSC 11 query went unanswered.
QUERY_SCHEME = "\x1b[?996n"


class AppearanceChanged(Message):
    """The terminal has told us which way round it is."""

    def __init__(self, appearance: Appearance) -> None:
        super().__init__()
        self.appearance = appearance

    def __rich_repr__(self):
        yield self.appearance


# --- reading a colour -----------------------------------------------------

_RE_XPARSE = re.compile(r"rgba?:([0-9a-f]+)/([0-9a-f]+)/([0-9a-f]+)", re.IGNORECASE)
_RE_HEX = re.compile(r"#((?:[0-9a-f]{3}){1,4})\b", re.IGNORECASE)


def luminance(red: float, green: float, blue: float) -> float:
    """Perceived brightness of a colour, each channel scaled to 0–1."""

    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def read_color(reply: str) -> Appearance | None:
    """Read a terminal's answer to OSC 11 and say which way it leans.

    Terminals answer in XParseColor's `rgb:` form, with anywhere between one
    and four hex digits per channel, and a few answer with plain `#rrggbb`.
    """

    if match := _RE_XPARSE.search(reply):
        channels = [int(part, 16) / (16 ** len(part) - 1) for part in match.groups()]
    elif match := _RE_HEX.search(reply):
        digits = match.group(1)
        width = len(digits) // 3
        top = 16**width - 1
        channels = [
            int(digits[index * width : (index + 1) * width], 16) / top
            for index in range(3)
        ]
    else:
        return None
    return "light" if luminance(*channels) > 0.5 else "dark"


def from_environment() -> Appearance | None:
    """`COLORFGBG`, the one environment variable that carries this.

    Set by rxvt, Konsole and a few others as `fg;bg` (or `fg;default;bg`), with
    the background as an ANSI colour index.
    """

    field = (os.environ.get("COLORFGBG") or "").split(";")[-1].strip()
    if not field.isdigit():
        return None
    # 0-6 and 8 are the dark half of the ANSI sixteen; 7 and 9-15 the light.
    return "dark" if int(field) in {0, 1, 2, 3, 4, 5, 6, 8} else "light"


def query_terminal(timeout: float = 0.2) -> Appearance | None:
    """Ask the terminal for its background colour, and wait briefly for a reply.

    Returns `None` for every kind of "no answer" there is: no controlling
    terminal, a terminal that ignores OSC 11, a reply we cannot read. The
    terminal's settings are always put back, including when the read fails.
    """

    if termios is None:
        return None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None

    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        os.close(fd)
        return None

    try:
        tty.setraw(fd)
        os.write(fd, (QUERY_BACKGROUND + QUERY_ATTRIBUTES).encode())
        reply = _read_reply(fd, timeout)
        return read_color(reply)
    except OSError:
        return None
    finally:
        try:
            # Anything still queued is our own echo, and would otherwise be
            # read as keystrokes the moment the app starts.
            termios.tcflush(fd, termios.TCIFLUSH)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass
        os.close(fd)


#: DA1's reply, which arrives after the OSC 11 one and means "that is all".
_RE_ATTRIBUTES = re.compile(r"\x1b\[\?[\d;]*c")


def _read_reply(fd: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    reply = ""
    while (remaining := deadline - time.monotonic()) > 0:
        if not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, 256)
        if not chunk:
            break
        reply += chunk.decode("utf-8", "replace")
        if _RE_ATTRIBUTES.search(reply):
            break
    return reply


def detect(setting: str = "auto") -> Appearance:
    """Which palette to start in.

    `"dark"` and `"light"` are taken at their word and cost nothing. `"auto"`
    asks the terminal, falls back to the environment, and settles on dark —
    which is what `dai` has always looked like — when neither will say.
    """

    if setting in ("dark", "light"):
        return setting  # type: ignore[return-value]
    return query_terminal() or from_environment() or "dark"


# --- taking our sequences out of the input stream -------------------------

#: `CSI ? 997 ; 1 n` is dark, `; 2 n` is light.
_RE_SCHEME = re.compile(r"\x1b\[\?997;([12])n")

#: An OSC 10 or 11 reply, terminated by BEL or ST. OSC 10 is the foreground; we
#: never ask for it, but a terminal that volunteers it must not reach the keys.
_RE_COLOR_REPLY = re.compile(r"\x1b\]1[01];[^\x07\x1b]*(?:\x07|\x1b\\)")

#: The two complete forms of the scheme report, used to recognise a fragment of
#: one that a read happened to cut in half.
_SCHEME_FORMS = ("\x1b[?997;1n", "\x1b[?997;2n")
_COLOR_HEAD = "\x1b]11;"

#: Past this a fragment is not a sequence of ours, it is junk, and holding on to
#: it only delays whatever comes next. Textual gives up at the same length.
_MAX_FRAGMENT = 32


def _extract(data: str) -> tuple[str, list[Appearance]]:
    """Take our sequences out of a chunk, and report what they said."""

    found: list[Appearance] = []

    def scheme(match: re.Match[str]) -> str:
        found.append("dark" if match.group(1) == "1" else "light")
        return ""

    def color(match: re.Match[str]) -> str:
        if (appearance := read_color(match.group(0))) is not None:
            found.append(appearance)
        return ""

    data = _RE_SCHEME.sub(scheme, data)
    data = _RE_COLOR_REPLY.sub(color, data)
    return data, found


def _fragment_at_end(data: str) -> int:
    """Length of a trailing fragment that may still become one of ours.

    A read can land in the middle of a sequence. What must *not* happen is
    holding back a bare `\\x1b`: that is the escape key, which cancels the
    prompt and dismisses every modal, and it would sit there until the next
    keystroke. So only a fragment long enough to be unmistakably ours is held —
    and `\\x1b[?2026;…`, which Textual reads for its own purposes, is not.
    """

    start = data.rfind("\x1b")
    if start < 0:
        return 0
    tail = data[start:]
    if len(tail) < 3 or len(tail) > _MAX_FRAGMENT:
        return 0
    if any(form.startswith(tail) for form in _SCHEME_FORMS):
        return len(tail)
    if _COLOR_HEAD.startswith(tail) or tail.startswith(_COLOR_HEAD):
        return len(tail)
    return 0


class SchemeParser(XTermParser):
    """Textual's input parser, with the colour-scheme traffic removed.

    Anything Textual's parser does not recognise it eventually reissues as
    keypresses, so these have to come out before it sees them.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fragment = ""
        self._fragment_at = 0.0

    def feed(self, data: str) -> Iterable[Message]:
        if not data:
            # Textual's end-of-input signal. Anything held back is never going
            # to be completed now, so let it through as the keys it apparently
            # was, and then pass the signal on unchanged.
            yield from self._release()
            yield from super().feed(data)
            return

        data = self._fragment + data
        self._fragment = ""

        # Complete sequences come out first: otherwise a chunk that ends on one
        # would look exactly like a chunk cut off part-way through one.
        data, appearances = _extract(data)
        if held := _fragment_at_end(data):
            data, self._fragment = data[:-held], data[-held:]
            self._fragment_at = time.monotonic()

        for appearance in appearances:
            yield AppearanceChanged(appearance)
        if data:
            # An empty string means end of input to the parser, so a chunk that
            # was nothing but a report must not be handed on at all.
            yield from super().feed(data)

    def tick(self) -> Iterable[Message]:
        """Called between reads; also where a held fragment finally times out."""

        if self._fragment and time.monotonic() - self._fragment_at > _ESCAPE_DELAY:
            yield from self._release()
        yield from super().tick()

    def _release(self) -> Iterable[Message]:
        fragment, self._fragment = self._fragment, ""
        if fragment:
            yield from super().feed(fragment)


#: However long Textual is willing to wait for the rest of an escape sequence,
#: we should not hold one longer.
_ESCAPE_DELAY = constants.ESCAPE_DELAY


# --- the driver that asks to be told --------------------------------------


def driver_class():
    """Textual driver to use, or `None` to leave Textual's own choice alone.

    Only where Textual would have picked the Linux driver anyway, and only when
    there is a terminal to talk to. A `None` here costs nothing but live theme
    switching.
    """

    if sys.platform == "win32" or constants.DRIVER:
        return None
    if sys.__stdin__ is None or not sys.__stdin__.isatty():
        return None
    try:
        return _appearance_driver()
    except ImportError:  # pragma: no cover - POSIX only, like the rest of dai
        return None


@lru_cache(maxsize=1)
def _appearance_driver():
    """Build the driver class once, so it is the same class every time."""

    from textual.drivers.linux_driver import LinuxDriver

    class AppearanceDriver(LinuxDriver):
        """The Linux driver, asked to report colour-scheme changes."""

        def start_application_mode(self) -> None:
            super().start_application_mode()
            self.write(ENABLE_SCHEME_REPORTS)
            self.write(QUERY_SCHEME)
            self.flush()

        def stop_application_mode(self) -> None:
            self.write(DISABLE_SCHEME_REPORTS)
            self.flush()
            super().stop_application_mode()

        def run_input_thread(self) -> None:
            """Run Textual's input loop, but with our parser in it.

            The parser is constructed inside that loop, so lending it ours means
            rebinding the name it looks up. If a future Textual moves the name,
            the app runs on the stock parser: no live switching, no crash.
            """

            module = sys.modules.get(LinuxDriver.__module__)
            original = getattr(module, "XTermParser", None)
            if original is None:
                super().run_input_thread()
                return
            module.XTermParser = SchemeParser
            try:
                super().run_input_thread()
            finally:
                module.XTermParser = original

    return AppearanceDriver
