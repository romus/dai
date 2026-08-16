"""One screen, on its own, with data that never came from a run.

Cheap because none of the modals do any work of their own — `MergeScreen` in
particular "does no git; it is handed rows worked out before it opened", so
what you see here is not an approximation of that screen, it is that screen.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from dai.budget import Spend
from dai.models import Issue, Outcome, Severity
from dai.orchestrator import DebateResult
from dai.snapshot import MergeCandidate
from dai.tui.app import ConfirmQuitScreen, DeadlockScreen, InjectScreen, MergeScreen
from dai.tui.theme import apply_theme

RUN_BRANCH = "dai/20260814-235546-wpt4"


def _candidates(cwd: Path) -> list[MergeCandidate]:
    """Three repositories, one of which cannot go — every row state at once."""

    return [
        MergeCandidate(
            repo=cwd, label=".", branch=RUN_BRANCH, base_branch="main",
            added=318, removed=41, files=("index.html", "primes.html"),
        ),
        MergeCandidate(
            repo=cwd / "packages" / "engine", label="packages/engine",
            branch=RUN_BRANCH, base_branch="develop", added=96, removed=12,
            files=("solver.py", "arbiter.py") + tuple(f"round{n}.py" for n in range(9)),
        ),
        MergeCandidate(
            repo=cwd / "vendor" / "prompts", label="vendor/prompts",
            branch=RUN_BRANCH, base_branch="main", added=4, files=("critic.md",),
            refusal="critic.md was edited on main too",
        ),
    ]


def _stalled() -> DebateResult:
    return DebateResult(
        outcome=Outcome.DEADLOCK,
        reason="neither side moved for 2 round(s)",
        rounds=[],
        spend=Spend(turns=7, tokens=48_120, measured_usd=1.24),
        open_issues=[
            Issue(
                id="i1", severity=Severity.BLOCKER,
                claim="the status column is still empty for search",
                evidence="notes.md:6",
            ),
            Issue(
                id="i2", severity=Severity.MAJOR,
                claim="the port for auth contradicts the README",
                evidence="notes.md:4",
            ),
        ],
    )


class _Host(App):
    """A bare screen to push a modal onto.

    Registers the theme before the stylesheet is parsed — every colour in the
    sheet comes from the theme, so a host that skips this cannot load it.
    """

    CSS_PATH = Path(__file__).resolve().parent.parent / "tui" / "styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, screen_name: str, cwd: Path, appearance: str) -> None:
        super().__init__()
        apply_theme(self, appearance)
        self.screen_name = screen_name
        self.cwd = cwd

    def compose(self) -> ComposeResult:
        yield Static("")

    def on_mount(self) -> None:
        self.push_screen(self._build(), lambda _answer: self.exit())

    def _build(self):
        if self.screen_name == "merge":
            return MergeScreen(_candidates(self.cwd), run_branch=RUN_BRANCH)
        if self.screen_name == "deadlock":
            return DeadlockScreen(_stalled(), "critic")
        if self.screen_name == "inject":
            return InjectScreen(self.cwd)
        return ConfirmQuitScreen()


#: Everything `--preview` accepts. `task` is a whole App rather than a modal, so
#: it is run directly instead of being pushed onto a host.
NAMES = ("merge", "deadlock", "inject", "quit", "task")


def preview(name: str, *, cwd: Path, appearance: str = "dark") -> None:
    """Show one screen, and return when it is dismissed."""

    if name == "task":
        from dai.tui import ask_for_task

        answer = ask_for_task(cwd, appearance=appearance)
        print(f"task: {answer or '(cancelled)'}")
        return
    _Host(name, cwd, appearance).run()
