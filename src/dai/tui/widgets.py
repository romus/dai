"""Widgets for the debate screen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import RenderableType
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, RichLog, Static

from dai.models import AgentEvent, CriticTurn, Severity, Verdict
from dai.tui import theme
from dai.tui.theme import spaced

#: Names, never colours — here and in `_PHASE_COLOR` below. A table like this
#: is built once, when the module is imported, and the palette changes under it
#: whenever the terminal's theme does; so what it holds is what to ask for, and
#: the asking happens at render time.
_SEVERITY_STYLE = {
    Severity.BLOCKER: "error",
    Severity.MAJOR: "warning",
    Severity.MINOR: "muted",
}

#: What colour the phase pill takes. Anything unlisted stays neutral, which is
#: the right default: an unrecognised phase is not news.
_PHASE_COLOR = {
    "solving": "solver",
    "answering": "solver",
    "reviewing": "critic",
    "paused": "warning",
    "waiting for you": "warning",
    "killing agents": "error",
    "agreed": "success",
    "deadlocked": "warning",
    "out of budget": "warning",
    "out of rounds": "warning",
    "stopped": "muted",
    "failed": "error",
}


class _Cell(Static):
    """A slot that paints from a callback.

    Rendering late — rather than pushing content in with `update()` — keeps the
    status bar paintable before its children have mounted, which is exactly
    when the app first sets the round. It also means a theme change reaches it
    with nothing but a `refresh()`.
    """

    def __init__(self, paint: Callable[[], Text], **kwargs) -> None:
        super().__init__(**kwargs)
        self._paint = paint

    def render(self) -> RenderResult:
        return self._paint()


class StatusBar(Horizontal):
    """Round, phase and spend — the three things you glance up to check."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.round = 0
        self.max_rounds = 0
        self.phase = "starting"
        self.spent = 0.0
        self.limit: float | None = None
        self.exact = True
        self._brand = _Cell(self._render_brand, id="brand")
        self._progress = _Cell(self._render_progress, id="progress")
        self._pill = _Cell(self._render_pill, id="phase-pill")

    def compose(self) -> ComposeResult:
        yield self._brand
        yield self._progress
        yield self._pill

    def refresh_bar(self) -> None:
        self._brand.refresh(layout=True)
        self._progress.refresh(layout=True)
        self._pill.refresh(layout=True)

    def repaint(self) -> None:
        """A theme change needs nothing more: the cells paint from callbacks."""

        self.refresh_bar()

    def _render_brand(self) -> Text:
        return Text("dai", style=theme.style("strong"))

    def _render_progress(self) -> Text:
        money = f"${self.spent:.2f}"
        if self.limit:
            money += f"/${self.limit:.2f}"
        if not self.exact:
            money += "+"

        line = Text()
        line.append("│  ", style=theme.S_DIM_RULE)
        line.append("round ", style=theme.S_MUTED)
        line.append(f"{self.round}/{self.max_rounds}", style=theme.S_TEXT)
        line.append("  ")
        line.append_text(theme.meter(self.round, self.max_rounds))
        line.append(f"  {money}", style=theme.S_MUTED)
        return line

    def _render_pill(self) -> Text:
        color = theme.color(_PHASE_COLOR.get(self.phase, "muted"))
        line = Text()
        line.append("● ", style=color)
        line.append(spaced(self.phase.upper()), style=f"bold {color}")
        return line

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.refresh_bar()

    def set_round(self, number: int) -> None:
        self.round = number
        self.refresh_bar()

    def set_spend(self, spent: float, exact: bool) -> None:
        self.spent = spent
        self.exact = exact
        self.refresh_bar()


@dataclass(frozen=True)
class _Line:
    """One thing written to a pane's log, kept so it can be drawn again.

    A `RichLog` stores rendered strips with the colours already baked in, and
    Textual has no way to re-render them. So the pane remembers what it was
    asked to write — in style *names*, never resolved colours — and a theme
    change replays the lot.
    """

    kind: str
    text: str = ""
    detail: str = ""
    style: str = ""

    def render(self) -> tuple[RenderableType, bool]:
        """The renderable, and whether it should expand to the pane's width."""

        if self.kind == "rule":
            # A Rule rather than a run of dashes: it is measured against the
            # pane, so it reaches the edge and never wraps onto a second line.
            return (
                Rule(
                    Text(self.text, style=theme.S_MUTED),
                    characters="─",
                    style=theme.S_DIM_RULE,
                    align="left",
                ),
                True,
            )
        if self.kind == "tool":
            # The tool name is the label and the argument is the news, so the
            # argument gets the bright half of the line.
            line = Text(self.text, style=theme.S_MUTED)
            if self.detail:
                line.append(f" {self.detail}", style=theme.S_TEXT)
            return line, False
        if self.style:
            return Text(self.text, style=theme.style(self.style)), False
        return Text(self.text), False


class AgentPane(Vertical):
    """One side of the argument: a title and a live log of what it is doing."""

    def __init__(
        self,
        title: str,
        engine: str,
        cwd: Path,
        *,
        role: str = "solver",
        idle_text: str = "waiting to start",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.title_text = title
        self.engine = engine
        self.cwd = cwd
        # A palette field name rather than a colour: which side this is stays
        # true across a theme change, a hex does not.
        self.role = role
        self.idle_text = idle_text
        self._lines: list[_Line] = []
        self._pending_text = ""
        self._activity = ""
        self._pulse = False

    @property
    def accent(self) -> str:
        return theme.color(self.role)

    def compose(self) -> ComposeResult:
        yield Label(self._heading(), classes="pane-title")
        yield Static(self._idle(), classes="pane-idle")
        # min_width=1: the stock 78 renders every line at 78 columns and lets
        # the pane clip it, which in a half-width pane means losing the tail of
        # every sentence instead of wrapping it.
        yield RichLog(
            wrap=True,
            markup=False,
            auto_scroll=True,
            min_width=1,
            classes="pane-log",
        )
        yield _Cell(self._render_activity, classes="pane-activity")

    def on_mount(self) -> None:
        # Nothing has been said yet, so the placeholder holds the pane.
        self.query_one(".pane-log", RichLog).display = False
        self.query_one(".pane-activity", _Cell).display = False
        # Slow enough to read as a heartbeat rather than a flicker.
        self.set_interval(0.5, self._tick)

    @property
    def log_widget(self) -> RichLog:
        return self.query_one(RichLog)

    def _heading(self) -> Text:
        heading = Text()
        heading.append("● ", style=self.accent)
        heading.append(spaced(self.title_text), style=theme.style("strong"))
        heading.append(f"  ·  {self.engine}", style=theme.S_MUTED)
        return heading

    def _idle(self) -> Text:
        return Text(self.idle_text, style=theme.S_MUTED)

    # --- what the pane is doing right now ---------------------------------

    def set_activity(self, text: str | None) -> None:
        """Show, change or clear the live line under the log."""

        self._activity = text or ""
        self._pulse = bool(text)
        activity = self.query_one(".pane-activity", _Cell)
        activity.display = text is not None
        activity.refresh(layout=True)

    def _tick(self) -> None:
        activity = self.query_one(".pane-activity", _Cell)
        if not activity.display:
            return
        self._pulse = not self._pulse
        activity.refresh()

    def _render_activity(self) -> Text:
        line = Text()
        line.append("● ", style=self.accent if self._pulse else theme.S_MUTED)
        line.append(self._activity, style=theme.S_MUTED)
        return line

    # --- writing to the log -----------------------------------------------

    def _write(self, line: _Line) -> None:
        """Record what was asked for, then draw it."""

        self._lines.append(line)
        self._draw(line)

    def _draw(self, line: _Line) -> None:
        renderable, expand = line.render()
        self.log_widget.write(renderable, expand=expand)

    def repaint(self) -> None:
        """Draw the whole pane again, in whichever palette is active now."""

        if not self.is_mounted:
            return
        log = self.log_widget
        log.clear()
        for line in self._lines:
            self._draw(line)
        self.query_one(".pane-title", Label).update(self._heading())
        self.query_one(".pane-idle", Static).update(self._idle())
        self.query_one(".pane-activity", _Cell).refresh()

    def note(self, text: str, style: str = "") -> None:
        self._write(_Line("note", text=text, style=style))

    def rule(self, text: str) -> None:
        """Head a new turn — and retire the placeholder, since work has begun."""

        self.flush()
        self.query_one(".pane-idle", Static).display = False
        self.query_one(".pane-log", RichLog).display = True
        self._write(_Line("rule", text=text))

    def handle(self, event: AgentEvent) -> None:
        """Render one engine event.

        Streamed text arrives a few characters at a time, so it is buffered and
        emitted line by line — writing every fragment would scroll the pane into
        uselessness.
        """

        if event.kind == "text":
            self._pending_text += event.text
            while "\n" in self._pending_text:
                line, self._pending_text = self._pending_text.split("\n", 1)
                if line.strip():
                    self._write(_Line("note", text=line))
        elif event.kind == "tool":
            self.flush()
            self._write(
                _Line("tool", text=event.text, detail=self._shorten(event.detail))
            )
        elif event.kind == "thinking":
            self.flush()
            self._write(
                _Line("note", text=self._shorten(event.text), style="thinking")
            )
        elif event.kind == "error":
            self.flush()
            self._write(_Line("note", text=event.text, style="error"))

    def flush(self) -> None:
        if self._pending_text.strip():
            self._write(_Line("note", text=self._pending_text.strip()))
        self._pending_text = ""

    def _shorten(self, text: str, width: int = 120) -> str:
        shown = str(text).replace(f"{self.cwd}/", "").replace(str(self.cwd), ".")
        return shown if len(shown) <= width else shown[: width - 1] + "…"


@dataclass(frozen=True)
class _Entry:
    """One line of the running score, kept for the same reason as `_Line`."""

    kind: str
    text: str = ""
    style: str = ""
    label: str = ""
    number: int = 0
    engine: str = ""
    critic: CriticTurn | None = field(default=None, compare=False)


class VerdictLog(RichLog):
    """The running score: what each round decided."""

    #: Width of the label gutter, so continuation lines sit under the text.
    GUTTER = 10

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list[_Entry] = []

    def round_verdict(self, number: int, engine: str, critic: CriticTurn, note: str) -> None:
        self._add(
            _Entry("verdict", number=number, engine=engine, critic=critic, text=note)
        )

    def note(self, text: str, style: str = "", label: str = "") -> None:
        """One entry, optionally headed by a label in the gutter."""

        self._add(_Entry("note", text=text, style=style, label=label))

    def repaint(self) -> None:
        """Draw the whole score again, in whichever palette is active now."""

        self.clear()
        for entry in self._entries:
            self._draw(entry)

    def _add(self, entry: _Entry) -> None:
        self._entries.append(entry)
        self._draw(entry)

    def _draw(self, entry: _Entry) -> None:
        if entry.kind == "verdict":
            self._draw_verdict(entry)
        else:
            self._draw_note(entry)

    def _draw_verdict(self, entry: _Entry) -> None:
        critic = entry.critic
        assert critic is not None

        line = Text(" " * self.GUTTER)
        line.append(f"r{entry.number} ", style=theme.style("strong"))
        line.append(f"{entry.engine} ", style=theme.S_MUTED)
        if critic.verdict is Verdict.APPROVE:
            line.append("APPROVE", style=theme.S_SUCCESS)
        else:
            line.append("REQUEST_CHANGES", style=theme.S_WARNING)
            line.append(f" ({len(critic.open_issues)})", style=theme.WARNING)
        if entry.text:
            line.append(f" — {entry.text}", style=theme.S_MUTED)
        self.write(line)

        for issue in critic.open_issues[:4]:
            detail = Text(" " * (self.GUTTER + 5))
            detail.append(f"[{issue.id}] ", style=theme.S_MUTED)
            detail.append(issue.claim, style=theme.style(_SEVERITY_STYLE[issue.severity]))
            self.write(detail)

    def _draw_note(self, entry: _Entry) -> None:
        """A two-column grid rather than a padded string: a task long enough to
        wrap should stay in its column instead of falling back to the margin.
        """

        row = Table.grid(padding=0)
        row.add_column(width=self.GUTTER, no_wrap=True)
        row.add_column(ratio=1)
        row.add_row(
            Text(spaced(entry.label), style=theme.S_MUTED) if entry.label else "",
            Text(entry.text, style=theme.style(entry.style or "muted")),
        )
        self.write(row, expand=True)
