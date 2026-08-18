"""`@` path completion for the TUI's text inputs.

The prompt is the one thing a run hinges on, and a mistyped path in it costs a
full solver + critic round before anyone notices. This lets you pick paths from
the directory the agents will actually work in.

Search is hybrid: a bare `@` shows the top level, anything typed after it fuzzy
matches the whole tree. What gets inserted is a plain relative path — the `@` is
a typing trigger, not part of the task, since codex has no `@`-syntax and both
agents must receive identical text.

The text itself is multi-line: a task worth two agents arguing over rarely fits
on one line. `Enter` commits, `Shift+Enter` breaks the line, and `Ctrl+V` takes
whatever is on the system clipboard — a screenshot becomes an `[ImgN]` token,
text is simply inserted (see `clipboard.py`).
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.fuzzy import Matcher
from textual.message import Message
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option

from dai.tui import clipboard
from dai.tui.clipboard import Attachments
from dai.tui.theme import spaced

#: Beyond this the index stops being useful and starts being a liability.
MAX_ENTRIES = 20_000
DEFAULT_LIMIT = 50
DEFAULT_DEBOUNCE_MS = 80

#: Never worth offering, whatever the ignore settings say.
ALWAYS_SKIP = {".git", "__pycache__", ".dai"}

#: Only the first of these survives every terminal. `shift+enter` needs the
#: kitty keyboard protocol (Ghostty, kitty, WezTerm, recent iTerm2), which
#: Textual asks for but Terminal.app will not answer; the other two are what
#: those terminals can actually send.
NEWLINE_KEYS = {"shift+enter", "ctrl+j", "alt+enter"}


# --- the trigger rule -----------------------------------------------------


def active_mention(text: str, cursor: int) -> tuple[int, str] | None:
    """The `@…` fragment the cursor sits in, as ``(index_of_at, query)``.

    Returns None when there is no live mention, which is what closes the
    dropdown. The `@` must start a word: without that check an email address
    would open a file picker halfway through typing it.
    """

    before = text[:cursor]
    at = before.rfind("@")
    if at == -1:
        return None
    if at > 0 and not text[at - 1].isspace():
        return None
    fragment = before[at + 1 :]
    if any(char.isspace() for char in fragment):
        return None
    return at, fragment


# --- the candidate list ---------------------------------------------------


@dataclass(frozen=True)
class Entry:
    path: str
    is_dir: bool

    @property
    def display(self) -> str:
        return f"{self.path}/" if self.is_dir else self.path

    @property
    def depth(self) -> int:
        return self.path.count("/")


@dataclass
class PathIndex:
    """Every path worth offering, gathered once when the input opens."""

    root: Path
    entries: list[Entry] = field(default_factory=list)
    truncated: bool = False

    @classmethod
    def build(cls, root: Path, ignore: list[str] | None = None) -> PathIndex:
        files = _git_files(root)
        if files is None:
            files = _walk(root, set(ignore or []) | ALWAYS_SKIP)

        truncated = len(files) > MAX_ENTRIES
        files = sorted(files)[:MAX_ENTRIES]

        # git lists no directories, so derive them from the path prefixes.
        directories: set[str] = set()
        for path in files:
            parts = path.split("/")
            for depth in range(1, len(parts)):
                directories.add("/".join(parts[:depth]))

        entries = [Entry(path=p, is_dir=True) for p in sorted(directories)]
        entries += [Entry(path=p, is_dir=False) for p in files]
        return cls(root=root, entries=entries, truncated=truncated)


def _git_files(root: Path) -> list[str] | None:
    """Tracked plus new-but-not-ignored files, or None outside a repository.

    One call gets .gitignore honoured for free, which is the whole reason to
    prefer it over walking the tree.
    """

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    seen = {name for name in result.stdout.split("\0") if name}
    # A tracked file that has since been deleted is still listed; do not offer it.
    return [name for name in seen if (root / name).is_file()]


def _walk(root: Path, skip: set[str]) -> list[str]:
    """Fallback for directories that are not git repositories."""

    found: list[str] = []

    # Collect one past the cap so the caller can tell "exactly full" from
    # "overflowing" — stopping at the cap makes truncation undetectable.
    ceiling = MAX_ENTRIES + 1

    def descend(directory: Path) -> None:
        if len(found) >= ceiling:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name in skip or entry.is_symlink():
                continue
            if entry.is_dir():
                descend(entry)
            elif entry.is_file():
                found.append(entry.relative_to(root).as_posix())
                if len(found) >= ceiling:
                    return

    descend(root)
    return found


# --- the hybrid search ----------------------------------------------------


def suggest(index: PathIndex, query: str, limit: int = DEFAULT_LIMIT) -> list[Entry]:
    """Candidates for a query, newest rule first.

    An empty query lists the top level; a query ending in `/` lists that
    directory. Together those make picking a directory behave like navigation
    without needing a separate mode for it.
    """

    if not query:
        return _top_level(index)[:limit]

    if query.endswith("/"):
        children = _children(index, query.rstrip("/"))
        if children:
            return children[:limit]

    matcher = Matcher(query)
    scored = []
    for entry in index.entries:
        score = matcher.match(entry.path)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].path))
    return [entry for _, entry in scored[:limit]]


def _top_level(index: PathIndex) -> list[Entry]:
    shallow = [entry for entry in index.entries if entry.depth == 0]
    return sorted(shallow, key=lambda e: (not e.is_dir, e.path))


def _children(index: PathIndex, parent: str) -> list[Entry]:
    prefix = f"{parent}/"
    children = [
        entry
        for entry in index.entries
        if entry.path.startswith(prefix) and "/" not in entry.path[len(prefix) :]
    ]
    return sorted(children, key=lambda e: (not e.is_dir, e.path))


# --- the widget -----------------------------------------------------------


class PromptArea(TextArea):
    """The editable half of `CompletingInput`.

    A `TextArea` rather than an `Input`, for the newlines — but it keeps the
    `value` / `cursor_position` surface of an `Input` so the mention logic and
    everything reading the widget stay unchanged.

    Keys are intercepted here rather than through `BINDINGS` because a
    `TextArea` swallows `enter` and the arrows itself. `_on_key` runs before
    bindings, which makes it the only place the dropdown can win the argument.
    """

    @property
    def _owner(self) -> CompletingInput:
        assert isinstance(self.parent, CompletingInput)
        return self.parent

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, index: int) -> None:
        self.move_cursor(self.document.get_location_from_index(index))

    async def _on_key(self, event: events.Key) -> None:
        owner = self._owner

        if event.key in NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            if not self.read_only:
                start, end = self.selection
                self.replace("\n", start, end, maintain_selection_offset=False)
            return

        if event.key == "enter":
            event.stop()
            event.prevent_default()
            owner.commit()
            return

        if event.key == "escape":
            event.stop()
            event.prevent_default()
            owner.action_close()
            return

        if event.key == "ctrl+v":
            # `TextArea` binds this to its own `action_paste`, which pastes
            # Textual's internal buffer — not the system clipboard, and never a
            # picture. Intercepting here is what stops the two from racing.
            event.stop()
            event.prevent_default()
            if not self.read_only:
                await owner.paste()
            return

        if owner.is_open and event.key in ("up", "down", "tab"):
            event.stop()
            event.prevent_default()
            if event.key == "up":
                owner.action_previous()
            elif event.key == "down":
                owner.action_next()
            else:
                owner.action_accept()
            return

        await super()._on_key(event)


class CompletingInput(Vertical):
    """A text input that offers paths after `@`."""

    #: Fallbacks for when the text area is not the focused widget; while it is,
    #: `PromptArea._on_key` gets there first.
    BINDINGS = [
        Binding("down", "next", show=False),
        Binding("up", "previous", show=False),
        Binding("tab", "accept", show=False),
        Binding("escape", "close", show=False),
    ]

    class Submitted(Message):
        """Posted when the user commits the text, not a completion.

        Two texts, because they are no longer the same one: `value` is what the
        user typed and can still see, `prompt` is what the agents receive, with
        every `[ImgN]` swapped for the file it stands for.
        """

        def __init__(self, value: str, prompt: str | None = None) -> None:
            self.value = value
            self.prompt = value if prompt is None else prompt
            super().__init__()

    class Cancelled(Message):
        """Posted when escape is pressed with no dropdown left to close."""

    def __init__(
        self,
        *,
        cwd: Path,
        placeholder: str = "",
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        ignore: list[str] | None = None,
        attachments: Attachments | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.cwd = Path(cwd)
        self.placeholder = placeholder
        self.debounce_ms = max(0, debounce_ms)
        self.ignore = ignore
        #: Where a pasted picture goes. `None` disables that half of `ctrl+v`
        #: — a read-only workspace, or the demo, which writes nothing at all.
        self.attachments = attachments
        self._index: PathIndex | None = None
        self._visible: list[Entry] = []

    def compose(self) -> ComposeResult:
        yield PromptArea(
            placeholder=self.placeholder,
            soft_wrap=True,
            compact=True,
            highlight_cursor_line=False,
            tab_behavior="focus",
            id="completing-input",
        )
        yield Static(spaced("PATH"), id="completions-label")
        yield OptionList(id="completions")

    def on_mount(self) -> None:
        self._set_visible(False)
        self._load_index()
        self.input.focus()

    # --- pieces -----------------------------------------------------------

    @property
    def input(self) -> PromptArea:
        return self.query_one("#completing-input", PromptArea)

    @property
    def _dropdown(self) -> OptionList:
        return self.query_one("#completions", OptionList)

    def _set_visible(self, visible: bool) -> None:
        """The list and its label appear and disappear together."""

        self._dropdown.display = visible
        self.query_one("#completions-label", Static).display = visible

    @property
    def value(self) -> str:
        return self.input.value

    @property
    def prompt(self) -> str:
        """The text as the agents will read it: tokens resolved to paths."""

        if self.attachments is None:
            return self.value
        return self.attachments.resolve(self.value)

    @property
    def is_open(self) -> bool:
        return self._dropdown.display

    # --- pasting ----------------------------------------------------------

    async def paste(self) -> None:
        """Take whatever the system clipboard holds, picture or text.

        A picture first, because that is the reading a plain `ctrl+v` cannot
        already give you: the terminal will hand over text by itself and never
        an image. What lands in the box is the token, not the path — the path
        is nobody's idea of readable, and `prompt` puts it back at send time.
        """

        if self.attachments is not None and (token := await self.attachments.grab()):
            self.input.insert(f"{token} ")
            return

        if text := await clipboard.read_text():
            self.input.insert(text)

    # --- reacting to typing ----------------------------------------------

    @work
    async def _load_index(self) -> None:
        """Scanning a cold monorepo must not freeze the keyboard."""

        self._index = await asyncio.to_thread(PathIndex.build, self.cwd, self.ignore)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        mention = active_mention(self.input.value, self.input.cursor_position)
        if mention is None:
            self._close()
            return
        # Opening is immediate so the marker feels responsive; only filling the
        # list waits for the debounce.
        self._set_visible(True)
        self._refresh(mention[1])

    @work(exclusive=True)
    async def _refresh(self, query: str) -> None:
        if self.debounce_ms:
            # A newer keystroke cancels this worker mid-sleep, so the delay is
            # enforced without a timer to track or a stale result to discard.
            await asyncio.sleep(self.debounce_ms / 1000)
        if self._index is None:
            return
        self._show(suggest(self._index, query), query)

    def _show(self, entries: list[Entry], query: str) -> None:
        self._visible = entries
        dropdown = self._dropdown
        dropdown.clear_options()
        if not entries:
            self._set_visible(False)
            return

        matcher = Matcher(query) if query and not query.endswith("/") else None
        dropdown.add_options(
            [
                Option(matcher.highlight(entry.display) if matcher else Content(entry.display))
                for entry in entries
            ]
        )
        dropdown.highlighted = 0
        self._set_visible(True)

    def _close(self) -> None:
        self._set_visible(False)
        self._dropdown.clear_options()
        self._visible = []

    # --- committing -------------------------------------------------------

    def commit(self) -> None:
        """Enter completes while the list is open, and only then submits."""

        if self.is_open and self._visible:
            self.action_accept()
            return
        self.post_message(self.Submitted(self.value, self.prompt))

    def action_accept(self) -> None:
        if not self.is_open or not self._visible:
            return
        highlighted = self._dropdown.highlighted
        if highlighted is None:
            return
        entry = self._visible[highlighted]

        text = self.input.value
        mention = active_mention(text, self.input.cursor_position)
        if mention is None:
            return
        start, query = mention
        end = start + 1 + len(query)

        # A directory keeps the `@` so completion survives into it; a file drops
        # the marker, because that is what the agents receive.
        replacement = f"@{entry.path}/" if entry.is_dir else entry.path
        # An edit rather than a reload: picking a path stays undoable, and the
        # cursor lands after what was inserted.
        document = self.input.document
        self.input.replace(
            replacement,
            document.get_location_from_index(start),
            document.get_location_from_index(end),
            maintain_selection_offset=False,
        )

        if entry.is_dir:
            self._refresh(f"{entry.path}/")
        else:
            self._close()

    def action_next(self) -> None:
        if self.is_open:
            self._dropdown.action_cursor_down()

    def action_previous(self) -> None:
        if self.is_open:
            self._dropdown.action_cursor_up()

    def action_close(self) -> None:
        """Close the list; the screen's own escape handling takes over after."""

        if self.is_open:
            self._close()
        else:
            self.post_message(self.Cancelled())
