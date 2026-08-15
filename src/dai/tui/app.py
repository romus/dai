"""The debate screen: solver on the left, critic on the right, verdicts below."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Label, Static
from textual.worker import Worker, WorkerCancelled, WorkerFailed

from dai.models import AgentEvent, Outcome, Role
from dai.orchestrator import Debate, DebateEvent, DebateResult
from dai.snapshot import Snapshotter
from dai.transcript import Transcript
from dai.tui import theme
from dai.tui.appearance import AppearanceChanged, driver_class
from dai.tui.completion import DEFAULT_DEBOUNCE_MS, CompletingInput
from dai.tui.theme import apply_theme, hint
from dai.tui.widgets import AgentPane, StatusBar, VerdictLog

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


class DeadlockScreen(ModalScreen[str]):
    """Shown when neither side will move. Nothing proceeds until you decide."""

    #: Only these are answers; "ask" is the question, not a reply to it.
    CHOICES = ("critic", "solver")

    def __init__(self, result: DebateResult, default: str) -> None:
        super().__init__()
        self.result = result
        # policy="ask" is precisely how we got here, so it cannot also be the
        # pre-selected answer — focusing a button that does not exist crashes
        # the screen at the moment the user is most needed.
        self.default = default if default in self.CHOICES else "critic"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("They will not converge", classes="title")
            yield Static(self.result.reason)
            with VerticalScroll(id="deadlock-issues"):
                for issue in self.result.open_issues:
                    line = Text()
                    line.append(f"[{issue.id}] ", style=theme.S_MUTED)
                    line.append(f"({issue.severity.value}) ", style=theme.WARNING)
                    line.append(issue.claim, style=theme.S_TEXT)
                    yield Static(line)
                    if issue.evidence:
                        yield Static(
                            Text(f"      {issue.evidence}", style=theme.S_MUTED)
                        )
            with Horizontal():
                yield Button("Critic is right", variant="warning", id="critic")
                yield Button("Solver is right", variant="primary", id="solver")
            yield Static(
                Text(
                    f"default for this run: {self.default} wins",
                    style=theme.S_MUTED,
                )
            )

    def on_mount(self) -> None:
        self._focus_default()

    def repaint(self) -> None:
        """The issue list bakes its colours in, so a theme change rebuilds it.

        This screen can sit there for as long as the user takes to decide, so
        it is the one modal that really can outlive the palette it was drawn in.
        """

        self.refresh(recompose=True)
        self.call_after_refresh(self._focus_default)

    def _focus_default(self) -> None:
        self.query_one(f"#{self.default}", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or self.default)


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
            verdicts.note(
                f"committing {len(self.snapshotter.repos)} repo(s) round by round "
                f"→ {self.snapshotter.branch}"
            )

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

        path = self.transcript.finish(
            self.result,
            task=self.debate.task,
            cwd=self.cwd,
            solver=self.debate.solver.name,
            critic=self.debate.critic.name,
            branch=self.snapshotter.report_branch,
        )
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

    async def _on_deadlock(self, pending: DebateResult) -> str:
        """Stop everything and let the user call it."""

        self.query_one(StatusBar).set_phase("waiting for you")
        choice = await self.push_screen_wait(
            DeadlockScreen(pending, self.debate.deadlock_policy)
        )
        self.query_one(VerdictLog).note(
            f"you chose: {choice} wins", style="strong"
        )
        return choice or self.debate.deadlock_policy

    # --- actions ----------------------------------------------------------

    def action_stop_debate(self) -> None:
        if self.result is not None or (
            self._runner is not None and self._runner.is_finished
        ):
            # Finished — or crashed and already announced. Nothing left to kill.
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
            # half-modified tree the confirmation warned about.
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
                branch=self.snapshotter.report_branch,
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
