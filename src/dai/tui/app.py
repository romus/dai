"""The debate screen: solver on the left, critic on the right, verdicts below."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, Static
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from dai.config import Merge
from dai.models import AgentEvent, Outcome, Role
from dai.orchestrator import Debate, DebateEvent, DebateResult
from dai.snapshot import MergeCandidate, Snapshotter, describe, merge_promise
from dai.transcript import Transcript
from dai.tui import theme
from dai.tui.appearance import AppearanceChanged, driver_class
from dai.tui.completion import DEFAULT_DEBOUNCE_MS, CompletingInput
from dai.tui.theme import apply_theme, hint, spaced
from dai.tui.widgets import (
    AgentPane,
    Cell,
    IssueCase,
    IssueRow,
    RepoRow,
    StatusBar,
    VerdictLog,
)

#: How each ending is announced. Style names, not style strings — the palette
#: they resolve against is whichever the terminal is wearing at the time.
_OUTCOME_STYLE = {
    Outcome.CONSENSUS: ("AGREED", "success"),
    Outcome.DEADLOCK: ("DEADLOCKED", "warning"),
    Outcome.BUDGET: ("OUT OF BUDGET", "warning"),
    Outcome.ROUNDS: ("OUT OF ROUNDS", "warning"),
    Outcome.ABORTED: ("STOPPED", "strong"),
    Outcome.FAILED: ("FAILED", "error"),
}

#: What the pane says about itself while that side holds the turn.
_ACTIVITY = {Role.SOLVE: "working", Role.CRITIQUE: "reviewing", Role.REBUT: "answering"}
_PHASE = {Role.SOLVE: "solving", Role.CRITIQUE: "reviewing", Role.REBUT: "answering"}


class FollowsTerminal:
    """Wears whichever palette the terminal is wearing, and keeps up with it.

    Mixed into both apps. The terminal announces a change on stdin, the driver
    turns it into an `AppearanceChanged`, and switching `App.theme` does the
    rest — for everything except the text the widgets have already drawn, which
    is what `repaint` is for.
    """

    def on_appearance_changed(self, message: AppearanceChanged) -> None:
        self.theme = theme.theme_name(message.appearance)

    def watch_theme(self, name: str) -> None:
        # Setting the theme in __init__ trips this before there is anything to
        # repaint — or any message pump to schedule it on.
        if not self.is_running:
            return
        theme.use("light" if name == theme.LIGHT_THEME.name else "dark")
        # Textual queues its own stylesheet refresh on this same list when the
        # theme changes, and it was queued first, so by the time we run the new
        # colours have reached the CSS.
        self.call_next(self.repaint)

    def repaint(self) -> None:
        """Draw everything again in the palette that is now active.

        Overridden by each app. A no-op rather than an error if one forgets:
        the wrong colours are worth less than a crash halfway through a debate.
        """


class InjectScreen(ModalScreen[str]):
    """A seat at the table for the human watching."""

    BINDINGS = [Binding("escape", "dismiss_empty", "cancel")]

    def __init__(self, cwd: Path, debounce_ms: int = DEFAULT_DEBOUNCE_MS) -> None:
        super().__init__()
        self.cwd = cwd
        self.debounce_ms = debounce_ms

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Say something to the agents — it outranks both of them:")
            yield CompletingInput(
                cwd=self.cwd,
                debounce_ms=self.debounce_ms,
                placeholder="e.g. the Status column in @… must stay untouched",
                id="inject",
            )

    def on_completing_input_submitted(self, event: CompletingInput.Submitted) -> None:
        self.dismiss(event.value)

    def on_completing_input_cancelled(self, event: CompletingInput.Cancelled) -> None:
        self.dismiss("")

    def action_dismiss_empty(self) -> None:
        self.dismiss("")


class DeadlockScreen(ModalScreen[dict]):
    """Neither side will move. You rule on each complaint, and the run goes on.

    Not a verdict on the run: a ruling is evidence the argument did not have.
    What you uphold goes back to the solver as binding instructions, what you
    dismiss leaves the argument for good, and the critic reviews what comes
    back — so a run headed for disagreement can still end in agreement.

    Answers with a `Ruling`: fingerprint -> "critic" | "solver". Fingerprints,
    not ids, because the answer outlives this critique and ids are renumbered
    freely between rounds.
    """

    AUTO_FOCUS = ""
    MAX_ROWS = 24

    VERTICAL_BREAKPOINTS = [(0, "-short"), (24, "-tall")]

    BINDINGS = [
        Binding("up,k", "cursor(-1)", "up", show=False),
        Binding("down,j", "cursor(1)", "down", show=False),
        Binding("left,c", "rule('critic')", "critic is right"),
        Binding("right,s", "rule('solver')", "solver is right"),
        Binding("enter", "carry_on", "continue"),
        # Consumed here for the same reason MergeScreen consumes it: the app's
        # `q` would cancel the very worker awaiting this screen.
        Binding("escape,q", "give_up", "use the default"),
    ]

    def __init__(self, result: DebateResult, default: str, *, stalled: int = 2) -> None:
        super().__init__()
        self.result = result
        # "ask" is how we got here, so it cannot also be the fallback.
        self.default = default if default in ("critic", "solver") else "critic"
        self.stalled = stalled
        self.cases = _read_the_argument(result)
        self.rulings: dict[str, str] = {}
        self._cursor = 0

    # --- what it says -----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="deadlock-card"):
            yield Cell(self._paint_outcome, id="deadlock-outcome")
            yield Cell(self._paint_title, id="deadlock-title")
            yield Cell(self._paint_lede, id="deadlock-lede")
            with _RowList(id="deadlock-issues"):
                for index, case in enumerate(self.cases):
                    yield IssueRow(case, id=f"issue-{index}")
            with Horizontal(id="deadlock-actions"):
                yield Button(self._go_label(), variant="primary", id="deadlock-go")
                yield Cell(self._paint_left, id="deadlock-left")
            yield Cell(self._paint_footnote, id="deadlock-footnote")

    def on_mount(self) -> None:
        for button in self.query(Button):
            button.can_focus = False
        self._sync()

    def on_resize(self, event: events.Resize) -> None:
        self._fit()

    def _fit(self) -> None:
        """Same budgeting as the merge screen, and for the same reason.

        The card is `height: auto` under a `max-height`, which clamps without
        making anything inside give way — and what gets clipped is the bottom,
        where the way to answer lives.
        """

        card = self.query_one("#deadlock-card", Vertical)
        listing = self.query_one("#deadlock-issues", _RowList)
        chrome = sum(
            child.outer_size.height + child.styles.margin.height
            for child in card.children
            if child is not listing
        )
        budget = (self.size.height * 9) // 10 - 4 - chrome
        listing.styles.max_height = max(4, min(self.MAX_ROWS, budget))

    def repaint(self) -> None:
        """Not `recompose`: it would take the cursor and every ruling with it."""

        for cell in self.query(Cell):
            cell.refresh()
        for row in self.query(IssueRow):
            row.refresh()

    def _paint_outcome(self):
        colour = theme.color("warning")
        label = Text()
        label.append("● ", style=colour)
        label.append(spaced("DEADLOCK"), style=f"bold {colour}")
        # A grid rather than padding: the tally has to reach the right edge at
        # any width, and a `rjust` guessed against the screen wraps instead.
        row = Table.grid(expand=True)
        row.add_column(no_wrap=True)
        row.add_column(justify="right", ratio=1, no_wrap=True)
        row.add_row(
            label,
            Text(
                f"{len(self.rulings)} of {len(self.cases)} decided",
                style=theme.S_MUTED,
            )
            if self.cases
            else "",
        )
        return row

    def _paint_title(self) -> Text:
        count = len(self.cases)
        title = (
            "Nothing is on the table. Who is right?"
            if not count
            else f"{count} issue{'s are' if count != 1 else ' is'} still open. Who is right?"
        )
        return Text(title, style=theme.style("strong"))

    def _paint_lede(self) -> Text:
        # Never hardcode the stall: this screen also serves a run that ran out
        # of rounds or money, where nobody was stalling at all.
        head = (
            f"Neither side moved for {self.stalled} rounds. "
            if self.result.outcome is Outcome.DEADLOCK
            else f"{self.result.reason.capitalize()}. "
        )
        return Text(f"{head}Decide each, then continue.", style=theme.S_MUTED)

    def _paint_left(self) -> Text:
        left = len(self.cases) - len(self.rulings)
        if not left:
            return Text("all decided", style=theme.S_MUTED)
        return Text(f"{left} issue{'s' if left != 1 else ''} left", style=theme.S_MUTED)

    def _paint_footnote(self) -> Text:
        return Text(
            "Your calls become the arbiter's ruling for the next round.",
            style=theme.S_MUTED,
        )

    def _go_label(self) -> str:
        return "↵  Continue"

    # --- what it does -----------------------------------------------------

    def action_cursor(self, delta: int) -> None:
        if not self.cases:
            return
        self._cursor = (self._cursor + delta) % len(self.cases)
        self._sync()
        self.query_one(f"#issue-{self._cursor}", IssueRow).scroll_visible(animate=False)

    def action_rule(self, side: str) -> None:
        if not self.cases:
            return
        self.rulings[self.cases[self._cursor].issue.fingerprint] = side
        # Move to the next one still undecided, so a run of decisions is a run
        # of keystrokes; wrapping to an already-decided row would be a dead end.
        for step in range(1, len(self.cases) + 1):
            nxt = (self._cursor + step) % len(self.cases)
            if self.cases[nxt].issue.fingerprint not in self.rulings:
                self._cursor = nxt
                break
        self._sync()

    def action_carry_on(self) -> None:
        # Guarded on the count, not the button: enter must be inert while
        # anything is undecided whether or not a button happens to exist.
        if len(self.rulings) < len(self.cases):
            return
        self.dismiss(dict(self.rulings))

    def action_give_up(self) -> None:
        """Bail out, and hand the rest to the configured default."""

        answer = dict(self.rulings)
        for case in self.cases:
            answer.setdefault(case.issue.fingerprint, self.default)
        self.dismiss(answer)

    def _sync(self) -> None:
        for index, row in enumerate(self.query(IssueRow)):
            row.ruling = self.rulings.get(row.case.issue.fingerprint, "")
            row.at_cursor = index == self._cursor
        button = self.query_one("#deadlock-go", Button)
        button.disabled = len(self.rulings) < len(self.cases)
        for cell_id in ("#deadlock-outcome", "#deadlock-left"):
            self.query_one(cell_id, Cell).refresh()
        self.call_after_refresh(self._fit)

    def on_issue_row_ruled(self, event: IssueRow.Ruled) -> None:
        event.stop()
        self._cursor = list(self.query(IssueRow)).index(event.row)
        self.action_rule(event.side)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_carry_on()


def _read_the_argument(result: DebateResult) -> list[IssueCase]:
    """Pair each open complaint with the answer the solver gave it.

    Awkward on purpose, because the data is: within a round the solver moves
    before the critic, so `rounds[-1].solver` answers the ids of
    `rounds[-2].critic` — one round stale. The join is by `Issue.fingerprint`,
    which the deadlock condition itself guarantees is stable across exactly
    those two rounds, falling back to the id and then to nothing.

    Everything here is defensive: this screen also serves a run that ran out of
    rounds or money, where there may be one round, no rebuttal, and no answer
    to show at all.
    """

    rounds = list(result.rounds)
    solver = rounds[-1].solver if rounds else None
    earlier = rounds[-2].critic if len(rounds) > 1 else None
    by_fingerprint = {i.fingerprint: i for i in (earlier.issues if earlier else [])}

    cases = []
    for issue in result.open_issues:
        reply = None
        if solver is not None:
            twin = by_fingerprint.get(issue.fingerprint)
            reply = solver.reply_to(twin.id) if twin else None
            reply = reply or solver.reply_to(issue.id)
        cases.append(
            IssueCase(
                issue=issue,
                critic_says=issue.fix or issue.claim,
                solver_says=reply.detail if reply else "",
            )
        )
    return cases


class ConfirmQuitScreen(ModalScreen[bool]):
    """q mid-run is a kill switch now; make sure it was meant."""

    BINDINGS = [
        Binding("q,y", "confirm", "kill and quit"),
        Binding("escape,n", "keep", "keep running"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Kill the agents?", classes="title")
            yield Static(
                "The turn in flight is killed mid-stride — the working tree may "
                "be left half-modified. Finished rounds are already committed."
            )
            with Horizontal():
                yield Button("Kill and quit", variant="error", id="kill")
                yield Button("Keep running", variant="primary", id="keep")

    def on_mount(self) -> None:
        # Enter must not be destructive; q/y are the deliberate way out.
        self.query_one("#keep", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "kill")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_keep(self) -> None:
        self.dismiss(False)


class _RowList(VerticalScroll, can_focus=False, inherit_bindings=False):
    """The merge screen's rows: scrollable, but never focusable.

    Both halves matter. A focusable `VerticalScroll` binds up and down to its
    own scrolling, so one `tab` would take the cursor keys away from the
    screen — and there is nothing here to focus *for*, since the rows are not
    something you land on. The screen tracks the cursor itself.
    """


class MergeScreen(ModalScreen[tuple[Path, ...]]):
    """At the end of an agreed run: which repositories go home.

    Does no git. It is handed rows worked out before it opened and hands back
    the paths that were ticked, which is what makes "nothing is written until
    you choose" a fact about the code rather than a line in a footer.
    """

    #: Nothing here is focusable, on purpose — see BINDINGS.
    AUTO_FOCUS = ""

    #: Rows shown before the list starts scrolling, when there is room for them.
    MAX_ROWS = 18

    #: A short terminal drops the two lines that are prose rather than substance,
    #: and a narrow one drops the hint before it crushes the controls.
    VERTICAL_BREAKPOINTS = [(0, "-short"), (22, "-tall")]
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (74, "-wide")]

    BINDINGS = [
        Binding("up,k", "cursor(-1)", "up", show=False),
        Binding("down,j", "cursor(1)", "down", show=False),
        Binding("space", "toggle", "toggle"),
        Binding("enter", "merge", "merge"),
        # `q` is named here so that it is *consumed*. The app's own q is a kill
        # switch, and the thing it would kill is the worker awaiting this very
        # screen. A modal truncates the binding chain, so this is belt and
        # braces rather than the only guard — but it is the one that says so.
        Binding("escape,q", "keep", "keep the branches"),
    ]

    def __init__(
        self,
        candidates: Sequence[MergeCandidate],
        *,
        run_branch: str,
        outcome: str = "AGREED",
    ) -> None:
        super().__init__()
        self.candidates = tuple(candidates)
        self.run_branch = run_branch
        self.outcome = outcome
        # Everything that can go starts ticked: asking must default to the
        # answer `merge = true` would have given, so enter is one keystroke.
        self._selected = {row.repo for row in self.candidates if row.mergeable}
        self._cursor = next(
            (i for i, row in enumerate(self.candidates) if row.mergeable), 0
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="merge-card"):
            yield Cell(self._paint_outcome, id="merge-outcome")
            yield Cell(self._paint_title, id="merge-title")
            yield Cell(self._paint_lede, id="merge-lede")
            yield Cell(self._paint_from, id="merge-from")
            with _RowList(id="merge-rows"):
                for index, candidate in enumerate(self.candidates):
                    yield RepoRow(
                        candidate, run_branch=self.run_branch, id=f"merge-row-{index}"
                    )
            with Horizontal(id="merge-actions"):
                yield Button(self._merge_label(), variant="primary", id="merge-go")
                # A Button, not a keycap: same border, same box, so the pair
                # reads as one size — and a thing that looks pressable has to
                # be pressable. It acts on the row under the cursor, which is
                # the only row a click on it could unambiguously mean.
                yield Button("space  Toggle", id="merge-toggle")
                yield Cell(self._paint_keep, id="merge-keep")
            yield Cell(self._paint_footnote, id="merge-footnote")

    def on_mount(self) -> None:
        # Nothing on this screen may take focus. A focused Button would answer
        # `enter` with its own binding, so a click on Toggle would leave the
        # next `enter` pressing Toggle again instead of merging.
        for button in self.query(Button):
            button.can_focus = False
        self._sync()
        # The first fit has to wait for the children to have a real size.
        self.call_after_refresh(self._fit)

    def on_resize(self, event: events.Resize) -> None:
        self._fit()

    def _fit(self) -> None:
        """Hand the list the height the rest of the card is not using.

        The card is `height: auto` under a `max-height`, which clamps it without
        making anything inside give way — so past a certain terminal height the
        surplus is simply clipped, and what it clips is the bottom: the button,
        the way out, and the promise that nothing has been written yet. A
        question you cannot see how to answer is worse than a short list, and
        the list is the only part of this screen that can honestly be shortened,
        so it is the part that pays.

        The chrome is measured, not counted. A constant here would be a number
        nobody remembers to change, and the first line added to the card would
        start quietly cutting the answer off again.
        """

        card = self.query_one("#merge-card", Vertical)
        listing = self.query_one("#merge-rows", _RowList)
        # `outer_size` counts border and padding but not margin, and three of
        # these lines carry one — leave them out and the card overflows by
        # exactly that much, which is the button's bottom edge.
        chrome = sum(
            child.outer_size.height + child.styles.margin.height
            for child in card.children
            if child is not listing
        )
        # The 90% and the border-plus-padding mirror `MergeScreen > Vertical`.
        budget = (self.size.height * 9) // 10 - 4 - chrome
        listing.styles.max_height = max(2, min(self.MAX_ROWS, budget))

    def repaint(self) -> None:
        """Draw every line again, in whichever palette is active now.

        Deliberately not `recompose`, which `DeadlockScreen` can afford and
        this cannot: it would rebuild the rows and take the cursor, the ticks
        and the scroll position with them. Nothing here holds a baked colour —
        the header paints from callbacks and the rows from `render()` — so
        asking each to draw again is the whole job. The tint, the hairlines and
        the card are `$dai-*` in the sheet, and were swapped before we ran.
        """

        for cell in self.query(Cell):
            cell.refresh()
        for row in self.query(RepoRow):
            row.refresh()

    # --- what it says -----------------------------------------------------

    def _paint_outcome(self) -> Text:
        color = theme.color("success")
        line = Text()
        line.append("● ", style=color)
        line.append(spaced(self.outcome), style=f"bold {color}")
        return line

    def _paint_title(self) -> Text:
        count = len(self.candidates)
        title = (
            "1 repository changed. Merge it?"
            if count == 1
            else f"{count} repositories changed. Merge which?"
        )
        return Text(title, style=theme.style("strong"))

    def _paint_lede(self) -> Text:
        return Text(
            "Each merges into the branch it was taken from.", style=theme.S_MUTED
        )

    def _paint_from(self) -> Text:
        line = Text()
        line.append(f"{spaced('FROM')}  ", style=theme.S_MUTED)
        line.append(self.run_branch, style=theme.S_TEXT)
        if tail := self._branch_tail():
            line.append(f"  ·  {tail}", style=theme.S_MUTED)
        return line

    def _branch_tail(self) -> str:
        """`· same name in all 3` — but only when it is true.

        A repository whose git would not take a slashed name took a flat one,
        and then the headline is not the whole story; those rows say their own
        branch instead, so this says nothing rather than something wrong.
        """

        names = {row.branch for row in self.candidates}
        if len(self.candidates) < 2 or names != {self.run_branch}:
            return ""
        return "same name in both" if len(self.candidates) == 2 else (
            f"same name in all {len(self.candidates)}"
        )

    def _paint_keep(self) -> Text:
        line = Text()
        line.append("esc ", style=theme.S_MUTED)
        line.append("Keep the branches", style=theme.style("strong"))
        return line

    def _paint_footnote(self) -> Text:
        return Text(
            "Nothing is written until you choose. Every branch stays either way.",
            style=theme.S_MUTED,
        )

    def _merge_label(self) -> str:
        return f"↵  Merge {len(self._selected)} selected"

    # --- what it does -----------------------------------------------------

    def action_cursor(self, delta: int) -> None:
        if not self.candidates:
            return
        self._cursor = (self._cursor + delta) % len(self.candidates)
        self._sync()
        self.query_one(f"#merge-row-{self._cursor}", RepoRow).scroll_visible(
            animate=False
        )

    def action_toggle(self) -> None:
        if not self.candidates:
            return
        row = self.candidates[self._cursor]
        # The cursor may rest on a row that cannot go — its reason is the whole
        # point of showing it — but space there is inert rather than a lie.
        if not row.mergeable:
            return
        self._selected ^= {row.repo}
        self._sync()

    def action_merge(self) -> None:
        # Guarded on the count, not on the button: enter has to be inert when
        # there is nothing to merge whether or not a button happens to exist.
        if not self._selected:
            return
        self.dismiss(
            tuple(row.repo for row in self.candidates if row.repo in self._selected)
        )

    def action_keep(self) -> None:
        self.dismiss(())

    def _sync(self) -> None:
        """Push the screen's two facts — cursor and ticks — onto the widgets."""

        for index, row in enumerate(self.query(RepoRow)):
            row.selected = row.candidate.repo in self._selected
            row.at_cursor = index == self._cursor
        button = self.query_one("#merge-go", Button)
        button.label = self._merge_label()
        button.disabled = not self._selected

    def on_repo_row_picked(self, event: RepoRow.Picked) -> None:
        event.stop()
        self._cursor = list(self.query(RepoRow)).index(event.row)
        self.action_toggle()
        self._sync()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "merge-toggle":
            self.action_toggle()
        else:
            self.action_merge()


class TaskPrompt(FollowsTerminal, App[str]):
    """Asks what to do, when `dai` is started without a task."""

    CSS_PATH = "styles.tcss"
    TITLE = "dai"
    # Textual binds ctrl+p to its command palette unless told not to. It has
    # nothing to offer here but a theme switcher, and dai picks its own.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        cwd: Path,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        appearance: str = "dark",
    ) -> None:
        super().__init__(driver_class=driver_class())
        apply_theme(self, appearance)
        self.cwd = cwd
        self.debounce_ms = debounce_ms

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-card"):
            yield Static(self._title(), id="prompt-title")
            yield CompletingInput(
                cwd=self.cwd,
                debounce_ms=self.debounce_ms,
                placeholder="e.g. fill in the empty cells in docs/matrix.md",
            )
            yield Static(self._hints(), id="prompt-hints")

    def _title(self) -> Text:
        title = Text()
        title.append("●  ", style=theme.SOLVER)
        title.append("What should the agents do?", style=theme.style("strong"))
        return title

    def _hints(self) -> Text:
        return hint(
            ("enter", "to start"),
            ("shift+enter", "new line"),
            ("@", "to pick a path"),
            ("esc", "to cancel"),
        )

    def on_mount(self) -> None:
        # Both apps share one stylesheet, and an App is not a CSS selector, so
        # the prompt's screen has to say which one it is.
        self.screen.add_class("task-prompt")

    def repaint(self) -> None:
        self.query_one("#prompt-title", Static).update(self._title())
        self.query_one("#prompt-hints", Static).update(self._hints())

    def on_completing_input_submitted(self, event: CompletingInput.Submitted) -> None:
        self.exit(event.value.strip())

    def on_completing_input_cancelled(self, event: CompletingInput.Cancelled) -> None:
        self.exit("")

    def action_cancel(self) -> None:
        self.exit("")


def ask_for_task(
    cwd: Path,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    appearance: str = "dark",
) -> str:
    """Prompt for a task interactively; empty means the user backed out."""

    return TaskPrompt(cwd, debounce_ms, appearance).run() or ""


class DaiApp(FollowsTerminal, App):
    """Watch two agents argue, and step in when they need you."""

    CSS_PATH = "styles.tcss"
    TITLE = "dai"
    # Off, or the Footer grows a `^p palette` cell next to our own `p pause`.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("q", "stop_debate", "quit"),
        Binding("p", "toggle_pause", "pause"),
        Binding("i", "inject", "inject"),
        Binding("a", "accept", "accept"),
    ]

    def __init__(
        self,
        debate: Debate,
        *,
        cwd: Path,
        transcript: Transcript,
        snapshotter: Snapshotter,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        appearance: str = "dark",
    ) -> None:
        super().__init__(driver_class=driver_class())
        apply_theme(self, appearance)
        self.debate = debate
        self.cwd = cwd
        self.debounce_ms = debounce_ms
        self.transcript = transcript
        self.snapshotter = snapshotter
        self.result: DebateResult | None = None
        #: Where the report was written, for the caller to print after we exit.
        self.report_path: Path | None = None
        #: The result exists *and* has been recorded. Not the same as `result`:
        #: between the two sit the final commit, the merge and the transcript.
        self._settled = False
        self._paused = False
        self._runner: Worker[None] | None = None
        self._quitting = False
        self._active_role: Role | None = None

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status")
        with Horizontal(id="panes"):
            yield AgentPane(
                "SOLVER",
                self.debate.solver.name,
                self.cwd,
                role="solver",
                idle_text="waiting to start",
                id="solver",
            )
            yield AgentPane(
                "CRITIC",
                self.debate.critic.name,
                self.cwd,
                role="critic",
                idle_text="waiting for the solver",
                id="critic",
            )
        # min_width=1 so long lines wrap to the widget instead of being clipped
        # at RichLog's stock 78 columns.
        yield VerdictLog(id="verdicts", wrap=True, markup=False, min_width=1)
        yield Footer()

    def on_mount(self) -> None:
        status = self.query_one(StatusBar)
        status.max_rounds = self.debate.budget.limits.max_rounds
        status.limit = self.debate.budget.limits.max_usd
        status.set_round(1)

        verdicts = self.query_one(VerdictLog)
        verdicts.note(self.debate.task, style="text", label="TASK")
        if self.snapshotter.active:
            # Announced before the run: it moves the repo onto a branch.
            note = (
                f"any of {len(self.snapshotter.repos)} repo(s) that changes moves "
                f"→ {self.snapshotter.branch}, a commit per round"
            ) + merge_promise(self.snapshotter.settings.merge)
            verdicts.note(note)

        self.debate.on_event = self._on_debate_event
        self.debate.on_agent_event = self._on_agent_event
        self.debate.on_round_start = self._on_round_start
        self.debate.on_deadlock = self._on_deadlock
        self.transcript.start(
            task=self.debate.task,
            cwd=self.cwd,
            solver=self.debate.solver.name,
            critic=self.debate.critic.name,
        )
        self._runner = self.run_debate()

    # --- the run ----------------------------------------------------------

    @work
    async def run_debate(self) -> None:
        """Drive the argument.

        A plain async worker, so it shares the app's event loop: the debate's
        callbacks can touch widgets directly, with no thread marshalling.
        """

        try:
            self.result = await self.debate.run()
        except Exception as exc:  # a crash must not leave a frozen screen
            self.query_one(VerdictLog).note(f"dai failed: {exc}", style="error")
            return

        if self.snapshotter.active:
            report = await asyncio.to_thread(
                self.snapshotter.capture_final,
                f"{self.result.outcome.value}: {self.result.reason}",
            )
            self.transcript.snapshots(report)
            if self.result.agreed and self.snapshotter.settings.merge is not Merge.NEVER:
                await self._settle_merge()

        path = self.transcript.finish(
            self.result,
            task=self.debate.task,
            cwd=self.cwd,
            solver=self.debate.solver.name,
            critic=self.debate.critic.name,
            repos=self.snapshotter.summary(),
        )
        self.report_path = path
        # Only now is the run genuinely over. Until this flag is set, `q` must
        # go the long way round, or it exits between the result and the record.
        self._settled = True
        self._announce(self.result, path)

    def _announce(self, result: DebateResult, path: Path | None) -> None:
        for pane in self.query(AgentPane):
            pane.set_activity(None)

        label, style = _OUTCOME_STYLE[result.outcome]
        verdicts = self.query_one(VerdictLog)
        verdicts.note(f"{label} — {result.reason}", style=style)

        spend = result.spend
        money = f"${spend.usd:.2f}"
        if not spend.exact:
            money += f" + unmeasured ({', '.join(sorted(spend.unpriced))})"
        verdicts.note(f"{spend.turns} turns · {spend.tokens:,} tokens · {money}")
        for line in describe(self.snapshotter.summary(), self.cwd):
            verdicts.note(line)
        if path is not None:
            verdicts.note(f"transcript: {path}")
        verdicts.note("press q to close")

        self.query_one(StatusBar).set_phase(label.lower())

    # --- debate callbacks -------------------------------------------------

    def _on_debate_event(self, event: DebateEvent) -> None:
        self.transcript.event(
            event.kind,
            round=event.round,
            role=event.role.value if event.role else None,
            engine=event.engine,
            text=event.text,
        )
        status = self.query_one(StatusBar)

        if event.kind == "turn_start":
            status.set_round(event.round)
            status.set_phase(_PHASE.get(event.role, "working"))
            pane = self._pane_for(event.role)
            pane.rule(f"round {event.round}")
            pane.set_activity(_ACTIVITY.get(event.role, "working"))
            self._active_role = event.role
            self._focus_pane(event.role)

        elif event.kind == "turn_end":
            pane = self._pane_for(event.role)
            pane.flush()
            pane.set_activity(None)
            self._active_role = None
            spend = self.debate.budget.spend
            status.set_spend(spend.usd, spend.exact)
            if event.text:
                pane.note(event.text, style="error")

        elif event.kind == "verdict":
            rnd = self.debate.rounds[-1]
            if rnd.critic is not None:
                self.query_one(VerdictLog).round_verdict(
                    rnd.number,
                    self.debate.critic.name,
                    rnd.critic,
                    rnd.assessment.reason if rnd.assessment else "",
                )

        elif event.kind == "note":
            self.query_one(VerdictLog).note(f"! {event.text}", style="plain-warning")

    def _on_agent_event(self, role: Role, event: AgentEvent) -> None:
        self._pane_for(role).handle(event)

    async def _on_round_start(self, number: int) -> None:
        if not self.snapshotter.active:
            return
        report = await asyncio.to_thread(
            self.snapshotter.capture_gate, number, self.debate.last_verdict
        )
        self.transcript.snapshots(report)
        for note in report.skipped:
            self.query_one(VerdictLog).note(
                f"! commit skipped — {note}", style="plain-warning"
            )

    async def _on_deadlock(self, pending: DebateResult) -> dict:
        """Stop everything and let the user rule on it, issue by issue."""

        self.query_one(StatusBar).set_phase("waiting for you")
        ruling = await self.push_screen_wait(
            DeadlockScreen(
                pending,
                self.debate.deadlock_policy,
                stalled=self.debate.referee.no_progress_rounds,
            )
        ) or {}
        upheld = sum(1 for side in ruling.values() if side == "critic")
        self.query_one(VerdictLog).note(
            f"you upheld {upheld} of {len(ruling)}"
            if ruling
            else "you left it to the default",
            style="strong",
        )
        return ruling

    async def _settle_merge(self) -> None:
        """Decide what goes home, and move it. Only ever reached on agreement.

        Sits between the final commit and the transcript on purpose: after the
        commit, so the rows it shows are the real ones; before the record, so
        the report can state what actually happened rather than what the config
        intended.
        """

        if self.snapshotter.settings.merge is Merge.ALWAYS:
            await asyncio.to_thread(self.snapshotter.merge)
            return

        rows = await asyncio.to_thread(self.snapshotter.preview)
        if not rows:
            return

        self.query_one(StatusBar).set_phase("waiting for you")
        assert self.result is not None
        label, _ = _OUTCOME_STYLE[self.result.outcome]
        # `or ()` for the same reason `_on_deadlock` has its fallback: a screen
        # dismissed by anything other than its own two exits answers nothing,
        # and nothing must read as "keep the branches", never as consent.
        chosen = await self.push_screen_wait(
            MergeScreen(rows, run_branch=self.snapshotter.branch, outcome=label)
        ) or ()
        self.query_one(VerdictLog).note(
            f"you chose: merge {len(chosen)} of {len(rows)}"
            if chosen
            else "you chose: keep the branches",
            style="strong",
        )
        await asyncio.to_thread(
            self.snapshotter.merge, only=chosen, kept="you kept the branch"
        )

    # --- actions ----------------------------------------------------------

    def action_stop_debate(self) -> None:
        if self._settled or (self._runner is not None and self._runner.is_finished):
            # Recorded — or crashed and already announced. Nothing left to kill.
            # Deliberately not `self.result is not None`: it is set before the
            # final commit, the merge and the transcript, and exiting in that
            # window would lose all three.
            self.exit()
            return
        if self._quitting:
            return
        self.push_screen(ConfirmQuitScreen(), self._confirm_quit)

    def _confirm_quit(self, confirmed: bool | None) -> None:
        if confirmed:
            self._quitting = True
            self._force_quit()

    @work
    async def _force_quit(self) -> None:
        """Cancel the runner, reap the agents, salvage the record, leave.

        Runs while the app is still alive: App.exit() never awaits workers, and
        teardown's fire-and-forget cancellation would cut the engine's SIGTERM →
        SIGKILL escalation short. So: cancel, await, then exit.
        """

        self.query_one(StatusBar).set_phase("killing agents")
        self.query_one(VerdictLog).note("killing agents…", style="error")

        if self._runner is not None:
            self._runner.cancel()
            try:
                await self._runner.wait()
                # The debate crossed the line before the cancel landed;
                # run_debate's tail has already recorded and announced it.
                self.exit()
                return
            except (WorkerCancelled, WorkerFailed):
                pass

        if self.result is None:
            self.result = self.debate.abort_result("killed by the user")
        try:
            # A deliberate kill still deserves a final commit: it captures the
            # half-modified tree the confirmation warned about. It never merges,
            # and not merely because an aborted run did not agree: the debate
            # can cross the line while the cancel is in flight, leaving a
            # consensus result here — and pressing q must never end in a reset.
            if self.snapshotter.active:
                report = await asyncio.to_thread(
                    self.snapshotter.capture_final, "killed by the user"
                )
                self.transcript.snapshots(report)
            self.transcript.finish(
                self.result,
                task=self.debate.task,
                cwd=self.cwd,
                solver=self.debate.solver.name,
                critic=self.debate.critic.name,
                repos=self.snapshotter.summary(),
            )
        except Exception:
            pass  # recording must never stand between the user and the exit
        self.exit()

    def action_toggle_pause(self) -> None:
        if self.result is not None:
            return
        self._paused = not self._paused
        if self._paused:
            self.debate.pause()
            self.query_one(StatusBar).set_phase("paused")
            self.query_one(VerdictLog).note(
                "paused — press p to resume", style="plain-warning"
            )
        else:
            self.debate.resume()
            self.query_one(StatusBar).set_phase(_PHASE.get(self._active_role, "working"))
            self.query_one(VerdictLog).note("resumed")
        if self._active_role is not None:
            self._pane_for(self._active_role).set_activity(
                "paused" if self._paused else _ACTIVITY.get(self._active_role, "working")
            )

    def action_inject(self) -> None:
        if self.result is not None:
            return
        self.push_screen(InjectScreen(self.cwd, self.debounce_ms), self._injected)

    def _injected(self, message: str | None) -> None:
        if not message:
            return
        self.debate.inject(message)
        self.query_one(VerdictLog).note(f"you: {message}", style="strong-critic")

    def action_accept(self) -> None:
        """Call it done: take the work as it stands."""

        if self.result is not None:
            return
        self.debate.deadlock_policy = "solver"
        self.debate.stop()
        self.query_one(VerdictLog).note(
            "accepting the current state; stopping after this turn…", style="plain-warning"
        )

    # --- helpers ----------------------------------------------------------

    def repaint(self) -> None:
        """Redraw the screen in the new palette.

        The stylesheet has already followed the theme by the time this runs;
        what has not is every `Text` the widgets assembled by hand, which a
        `RichLog` keeps as finished pixels. Those get built again from what
        they were made of.
        """

        self.query_one(StatusBar).repaint()
        for pane in self.query(AgentPane):
            pane.repaint()
        self.query_one(VerdictLog).repaint()
        for screen in self.screen_stack:
            if (repaint := getattr(screen, "repaint", None)) is not None:
                repaint()

    def _pane_for(self, role: Role | None) -> AgentPane:
        target = "critic" if role is Role.CRITIQUE else "solver"
        return self.query_one(f"#{target}", AgentPane)

    def _focus_pane(self, role: Role | None) -> None:
        active = self._pane_for(role)
        for pane in self.query(AgentPane):
            pane.set_class(pane is active, "active")
