"""The debate screen, driven headlessly by Textual's test pilot."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from dai.budget import Budget, Limits
from dai.consensus import Referee
from dai.config import Merge, SnapshotConfig
from dai.models import AgentEvent, Outcome, Role, Severity, Issue
from dai.orchestrator import Debate, DebateResult
from dai.snapshot import MergeCandidate, Snapshotter, ignore_locally
from dai.transcript import Transcript
from rich.console import Console
from textual.app import App
from textual.color import Color, ColorParseError
from textual.widgets import Button, Label, RichLog, Static

from dai.tui import theme
from dai.tui.app import (
    ConfirmQuitScreen,
    DaiApp,
    DeadlockScreen,
    InjectScreen,
    MergeScreen,
)
from dai.tui.appearance import AppearanceChanged
from dai.tui.theme import apply_theme
from dai.tui.widgets import AgentPane, Cell, RepoRow, StatusBar, VerdictLog
from test_orchestrator import Scripted, approve, changes, replies, solved


class Hanging(Scripted):
    """Plays its script, then parks forever; records whether the park was cancelled."""

    def __init__(self, name, script):
        super().__init__(name, script)
        self.hung = asyncio.Event()
        self.cancelled = False

    async def run(self, prompt, **kwargs):
        if self.script:
            return await super().run(prompt, **kwargs)
        self.hung.set()
        try:
            await asyncio.Event().wait()  # never set: a turn that outlives patience
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def git_out(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git_out("init", "-q", "-b", "main", cwd=path)
    git_out("config", "user.email", "t@example.com", cwd=path)
    git_out("config", "user.name", "test", cwd=path)
    (path / "seed.txt").write_text("seed\n")
    git_out("add", "-A", cwd=path)
    git_out("commit", "-qm", "initial", cwd=path)
    return path


def make_app(tmp_path, solver_script, critic_script, **kwargs):
    solver = Scripted("solver-engine", solver_script)
    critic = Scripted("critic-engine", critic_script)
    return make_app_from(tmp_path, solver, critic, **kwargs)


def make_app_from(tmp_path, solver, critic, **kwargs):
    appearance = kwargs.pop("appearance", "dark")
    snapshotter = kwargs.pop(
        "snapshotter", Snapshotter(tmp_path, "run1", SnapshotConfig(enabled=False))
    )
    debate = Debate(
        task="fill in the table",
        cwd=tmp_path,
        solver=solver,
        critic=critic,
        budget=Budget(Limits(max_rounds=kwargs.pop("max_rounds", 5), max_usd=None,
                             max_wall_seconds=None)),
        **kwargs,
    )
    app = DaiApp(
        debate,
        cwd=tmp_path,
        transcript=Transcript(tmp_path, "run1"),
        snapshotter=snapshotter,
        appearance=appearance,
    )
    return app, debate


async def settle(app, tries: int = 60) -> None:
    """Wait for the debate worker to finish."""

    for _ in range(tries):
        if app.result is not None:
            await asyncio.sleep(0.05)
            return
        await asyncio.sleep(0.05)


# --- layout ---------------------------------------------------------------


async def test_both_sides_get_a_pane(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        panes = list(app.query(AgentPane))

        assert [p.id for p in panes] == ["solver", "critic"]
        assert panes[0].engine == "solver-engine"
        assert panes[1].engine == "critic-engine"
        await pilot.pause()


async def test_status_bar_tracks_round_and_spend(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()], max_rounds=7)

    async with app.run_test():
        await settle(app)
        status = app.query_one(StatusBar)

        assert status.max_rounds == 7
        assert status.round >= 1
        assert status.phase == "agreed"


async def test_the_outcome_is_announced_not_just_exited(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test():
        await settle(app)

        assert app.result is not None
        assert app.result.outcome is Outcome.CONSENSUS


async def test_each_agent_writes_to_its_own_pane(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test():
        await settle(app)
        solver_pane = app.query_one("#solver", AgentPane)
        critic_pane = app.query_one("#critic", AgentPane)

        app._on_agent_event(Role.SOLVE, AgentEvent("tool", "Read", "matrix.md"))
        app._on_agent_event(Role.CRITIQUE, AgentEvent("tool", "bash", "cat matrix.md"))

        assert solver_pane.log_widget.lines
        assert critic_pane.log_widget.lines


async def test_rebuttals_appear_on_the_solver_side(tmp_path):
    """REBUT is the solver talking, so it must not land in the critic's pane."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test():
        await settle(app)

        assert app._pane_for(Role.REBUT).id == "solver"
        assert app._pane_for(Role.SOLVE).id == "solver"
        assert app._pane_for(Role.CRITIQUE).id == "critic"


# --- pane rendering -------------------------------------------------------


def test_streamed_text_is_buffered_into_whole_lines(tmp_path):
    pane = AgentPane("SOLVER", "claude", tmp_path)

    class FakeLog:
        def __init__(self):
            self.written = []

        def write(self, item, **kwargs):
            self.written.append(str(item))

    log = FakeLog()
    pane.query_one = lambda *a, **k: log  # type: ignore[assignment]

    for chunk in ("Reading ", "the file", "\nDone"):
        pane.handle(AgentEvent("text", chunk))

    assert log.written == ["Reading the file"]  # "Done" has no newline yet
    pane.flush()
    assert log.written == ["Reading the file", "Done"]


def test_pane_shortens_paths_under_the_working_directory(tmp_path):
    pane = AgentPane("SOLVER", "claude", tmp_path)

    assert pane._shorten(f"{tmp_path}/docs/matrix.md") == "docs/matrix.md"


class PaneHost(App):
    """One pane, no debate — so its own state can be driven a step at a time."""

    CSS_PATH = Path(__file__).parent.parent / "src" / "dai" / "tui" / "styles.tcss"

    def __init__(self, cwd: Path) -> None:
        super().__init__()
        # The stylesheet's colours all come from the theme, so a host that
        # loads it has to register one first.
        apply_theme(self)
        self.cwd = cwd

    def compose(self):
        yield AgentPane(
            "CRITIC", "codex", self.cwd, idle_text="waiting for the solver", id="critic"
        )


async def test_a_pane_holds_its_placeholder_until_it_has_something_to_say(tmp_path):
    """An empty log is a blank rectangle; say what the side is waiting for."""

    app = PaneHost(tmp_path)
    async with app.run_test() as pilot:
        pane = app.query_one(AgentPane)
        idle = pane.query_one(".pane-idle", Static)
        log = pane.query_one(".pane-log", RichLog)

        assert idle.display and not log.display
        assert "waiting for the solver" in str(idle.content)

        pane.rule("round 1")
        await pilot.pause()

        assert log.display and not idle.display


async def test_the_working_line_appears_for_the_side_holding_the_turn(tmp_path):
    app = PaneHost(tmp_path)
    async with app.run_test() as pilot:
        pane = app.query_one(AgentPane)
        activity = pane.query_one(".pane-activity")

        assert not activity.display

        pane.set_activity("working")
        await pilot.pause()
        assert activity.display
        assert "working" in str(activity.render())

        pane.set_activity(None)
        await pilot.pause()
        assert not activity.display


# --- controls -------------------------------------------------------------


async def test_pause_and_resume_toggle_the_debate(tmp_path):
    app, debate = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        # After the run finishes, pause is a no-op by design.
        app.result = None
        await pilot.press("p")
        assert app._paused
        await pilot.press("p")
        assert not app._paused


async def test_injecting_forwards_the_message_to_the_debate(tmp_path):
    app, debate = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test():
        await settle(app)
        app.result = None
        app._injected("the Status column must stay untouched")

        assert debate._injections == ["the Status column must stay untouched"]


async def test_an_empty_injection_is_ignored(tmp_path):
    app, debate = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test():
        await settle(app)
        app._injected("")
        app._injected(None)

        assert debate._injections == []


async def test_accept_takes_the_work_as_it_stands(tmp_path):
    app, debate = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        app.result = None
        await pilot.press("a")

        assert debate.deadlock_policy == "solver"
        assert debate._stopped


async def test_q_closes_once_the_run_is_over(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        await pilot.press("q")
        await pilot.pause()

        assert not app.is_running


async def wait_for(condition, tries: int = 80) -> None:
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0.05)


async def test_q_mid_run_asks_then_kills(tmp_path):
    solver = Hanging("solver-engine", [])
    app, _ = make_app_from(tmp_path, solver, Scripted("critic-engine", [approve()]))

    async with app.run_test() as pilot:
        await asyncio.wait_for(solver.hung.wait(), timeout=5)
        await pilot.press("q")
        await wait_for(lambda: isinstance(app.screen, ConfirmQuitScreen))
        assert isinstance(app.screen, ConfirmQuitScreen), "q must ask first"

        await pilot.click("#kill")
        await wait_for(lambda: not app.is_running)
        assert not app.is_running

    assert solver.cancelled, "the hanging turn was never cancelled"
    assert app.result is not None
    assert app.result.outcome is Outcome.ABORTED
    assert "killed" in app.result.reason
    assert (tmp_path / ".dai" / "runs" / "run1" / "report.md").is_file()


async def test_the_kill_switch_never_merges_the_work(tmp_path):
    """Pressing q must not end in a `reset --hard`, however the run was left.

    The debate can cross the finish line while the cancel is in flight, so the
    kill path can be holding a consensus result — which is exactly when a merge
    guarded only on agreement would fire.
    """

    repo = make_repo(tmp_path)
    ignore_locally(repo, ".dai/")  # as the real entry point does, before anything writes
    snapshotter = Snapshotter(repo, "run1", SnapshotConfig(merge=Merge.ALWAYS))
    snapshotter.observe()  # the round-1 gate, before any agent moves
    (repo / "a.txt").write_text("the agents got this far\n")
    solver = Hanging("solver-engine", [])
    app, _ = make_app_from(
        repo, solver, Scripted("critic-engine", [approve()]), snapshotter=snapshotter
    )
    before = git_out("rev-parse", "HEAD", cwd=repo)

    async with app.run_test() as pilot:
        await asyncio.wait_for(solver.hung.wait(), timeout=5)
        await pilot.press("q")
        await wait_for(lambda: isinstance(app.screen, ConfirmQuitScreen))
        await pilot.click("#kill")
        await wait_for(lambda: not app.is_running)

    captured = git_out("rev-parse", "dai/run1", cwd=repo)

    assert captured and captured != before, "the kill should still have committed"
    assert git_out("rev-parse", "main", cwd=repo) == before, "but must not have merged"
    assert git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"
    assert [entry.commits for entry in snapshotter.summary()] == [1]
    assert not any(entry.merged for entry in snapshotter.summary())


async def test_declining_the_kill_changes_nothing(tmp_path):
    solver = Hanging("solver-engine", [])
    app, debate = make_app_from(tmp_path, solver, Scripted("critic-engine", [approve()]))

    async with app.run_test() as pilot:
        await asyncio.wait_for(solver.hung.wait(), timeout=5)
        await pilot.press("q")
        await wait_for(lambda: isinstance(app.screen, ConfirmQuitScreen))
        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, ConfirmQuitScreen)
        assert app.is_running
        assert app.result is None
        assert not debate._stopped, "declining must not arm the soft stop either"
        assert not solver.cancelled


async def test_kill_confirmed_after_the_run_finished_just_exits(tmp_path):
    """The dialog raced the finish line and lost: exit, do not re-record."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        app.push_screen(ConfirmQuitScreen(), app._confirm_quit)
        await pilot.pause()
        await pilot.click("#kill")
        await wait_for(lambda: not app.is_running)

        assert not app.is_running

    assert app.result.outcome is Outcome.CONSENSUS


# --- deadlock screen ------------------------------------------------------


async def test_deadlock_screen_reports_the_choice(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])
    pending = DebateResult(
        outcome=Outcome.DEADLOCK,
        reason="neither side moved",
        rounds=[],
        spend=app.debate.budget.spend,
        open_issues=[Issue(id="i1", severity=Severity.MAJOR, claim="still wrong",
                           evidence="m.md:1")],
    )

    async with app.run_test() as pilot:
        await settle(app)
        screen = DeadlockScreen(pending, "critic")
        app.push_screen(screen)
        await pilot.pause()
        await pilot.click("#solver")
        await pilot.pause()

        assert isinstance(screen, DeadlockScreen)


async def test_deadlock_screen_shows_the_open_issues(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])
    issue = Issue(id="i7", severity=Severity.BLOCKER, claim="table still empty",
                  evidence="matrix.md:4")
    pending = DebateResult(
        outcome=Outcome.DEADLOCK, reason="stuck", rounds=[],
        spend=app.debate.budget.spend, open_issues=[issue],
    )

    async with app.run_test() as pilot:
        await settle(app)
        screen = DeadlockScreen(pending, "critic")
        app.push_screen(screen)
        await pilot.pause()

        # Textual v6 exposes a Static's text as `.content`, not `.renderable`.
        rendered = " ".join(str(w.content) for w in screen.query(Static))
        assert "table still empty" in rendered
        assert "matrix.md:4" in rendered
        assert "i7" in rendered


async def test_a_real_deadlock_pauses_for_the_user(tmp_path):
    """policy=ask must reach the screen, not resolve itself quietly."""

    stuck = [changes("same complaint") for _ in range(4)]
    app, debate = make_app(
        tmp_path,
        [solved()] + [solved(responses=replies(("i1", "REJECTED"))) for _ in range(4)],
        stuck,
        referee=Referee(no_progress_rounds=2),
        deadlock_policy="ask",
    )

    async with app.run_test() as pilot:
        for _ in range(80):
            if isinstance(app.screen, DeadlockScreen):
                break
            await asyncio.sleep(0.05)
        assert isinstance(app.screen, DeadlockScreen), "the user was never asked"

        await pilot.click("#solver")
        await settle(app)

        assert app.result is not None
        assert "solver's version stands" in app.result.reason


# --- the merge screen -----------------------------------------------------


def agreed_run(tmp_path, merge=Merge.ASK, repos=("proj",)):
    """A run that agrees, over real repositories the agents really changed."""

    for name in repos:
        make_repo(tmp_path / name)
    ignore_locally(tmp_path / repos[0], ".dai/")
    snapshotter = Snapshotter(tmp_path, "run1", SnapshotConfig(merge=merge))
    snapshotter.observe()  # the round-1 gate, before any agent moves
    for name in repos:
        (tmp_path / name / "a.txt").write_text("the agents did this\n")
    app, _ = make_app(
        tmp_path, [solved()], [approve()], snapshotter=snapshotter
    )
    return app, snapshotter


async def reach_the_question(app, pilot):
    await wait_for(lambda: isinstance(app.screen, MergeScreen))
    assert isinstance(app.screen, MergeScreen), "the user was never asked"
    return app.screen


def drawn(widget, width: int = 90) -> str:
    """What a widget actually puts on the screen, as plain text.

    A `RepoRow` renders a Rich grid, and the right-aligned half of it only
    exists once something has measured it against a width — so asserting on
    the renderable itself would assert on a repr.
    """

    console = Console(width=width, no_color=True, legacy_windows=False)
    with console.capture() as captured:
        console.print(widget.render(), end="")
    return captured.get()


async def test_agreeing_asks_which_repositories_to_merge(tmp_path):
    """`merge = "ask"` must reach the screen, not decide on its own."""

    app, snapshotter = agreed_run(tmp_path, repos=("one", "two"))

    async with app.run_test() as pilot:
        screen = await reach_the_question(app, pilot)

        assert len(screen.query(RepoRow)) == 2
        # The question is on screen and nothing has been written yet.
        assert not any(entry.merged for entry in snapshotter.summary())

        await pilot.press("enter")
        await wait_for(lambda: app._settled)

    assert [entry.merged for entry in snapshotter.summary()] == [True, True]


async def test_keeping_the_branches_writes_nothing_and_says_so(tmp_path):
    app, snapshotter = agreed_run(tmp_path)
    repo = tmp_path / "proj"
    before = git_out("rev-parse", "main", cwd=repo)

    async with app.run_test() as pilot:
        await reach_the_question(app, pilot)
        await pilot.press("escape")
        await wait_for(lambda: app._settled)

    entry = snapshotter.summary()[0]

    assert entry.merged is False
    assert entry.note == "you kept the branch"
    assert git_out("rev-parse", "main", cwd=repo) == before
    assert git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"
    assert git_out("rev-parse", "dai/run1", cwd=repo) != before  # the work is there


async def test_unticking_a_repository_leaves_that_one_alone(tmp_path):
    app, snapshotter = agreed_run(tmp_path, repos=("one", "two"))

    async with app.run_test() as pilot:
        await reach_the_question(app, pilot)
        await pilot.press("space")  # untick the row the cursor starts on
        await pilot.press("enter")
        await wait_for(lambda: app._settled)

    rows = {entry.repo.name: entry for entry in snapshotter.summary()}

    assert rows["one"].merged is False
    assert rows["one"].note == "you kept the branch"
    assert rows["two"].merged is True


async def test_a_branch_that_moves_while_you_decide_is_still_refused(tmp_path):
    """Saying yes is permission, not proof: the check runs again at write time.

    The question can sit there for as long as you take, and the world does not
    stop while it does.
    """

    app, snapshotter = agreed_run(tmp_path)
    repo = tmp_path / "proj"

    async with app.run_test() as pilot:
        screen = await reach_the_question(app, pilot)
        assert all(row.candidate.mergeable for row in screen.query(RepoRow))

        # Somebody lands a commit on main while the question is on screen.
        git_out("commit", "-q", "--allow-empty", "-m", "meanwhile", cwd=repo)
        git_out("update-ref", "refs/heads/main", "HEAD", cwd=repo)
        await pilot.press("enter")
        await wait_for(lambda: app._settled)

    entry = snapshotter.summary()[0]

    assert entry.merged is False
    assert entry.note and entry.note != "you kept the branch"
    assert git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


async def test_a_dead_row_is_never_counted_in_the_button(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])
    rows = [
        MergeCandidate(
            repo=tmp_path / "one", label="one", branch="dai/run1",
            base_branch="main", added=3, removed=1, files=("a.txt",),
        ),
        MergeCandidate(
            repo=tmp_path / "two", label="two", branch="dai/run1",
            base_branch="main", added=4, files=("b.txt",),
            refusal="b.txt was edited on main too",
        ),
    ]

    async with app.run_test() as pilot:
        await settle(app)
        screen = MergeScreen(rows, run_branch="dai/run1")
        app.push_screen(screen)
        await pilot.pause()

        assert "Merge 1 selected" in str(screen.query_one("#merge-go", Button).label)

        screen._cursor = 1  # park on the row that cannot go
        screen.action_toggle()
        await pilot.pause()

        assert "Merge 1 selected" in str(screen.query_one("#merge-go", Button).label)
        assert screen._selected == {tmp_path / "one"}
        assert "merge by hand" in drawn(list(screen.query(RepoRow))[1])


async def test_the_question_shows_what_would_be_written(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])
    rows = [
        MergeCandidate(
            repo=tmp_path / "one", label=".", branch="dai/run1", base_branch="main",
            added=318, removed=41, files=("index.html", "primes.html"),
        )
    ]

    async with app.run_test() as pilot:
        await settle(app)
        screen = MergeScreen(rows, run_branch="dai/run1")
        app.push_screen(screen)
        await pilot.pause()

        shown = " ".join(drawn(w) for w in screen.query(RepoRow))
        header = " ".join(str(w.render()) for w in screen.query(Cell))

        assert "index.html" in shown and "primes.html" in shown
        assert "+318" in shown and "-41" in shown
        assert ". " in shown and "main" in shown  # the repo, and where it goes
        assert "1 repository changed. Merge it?" in header
        assert "dai/run1" in header
        assert "Nothing is written until you choose" in header


async def test_q_on_the_merge_screen_keeps_the_branches_rather_than_killing(tmp_path):
    """The app's q would cancel the very worker that is awaiting this screen."""

    app, snapshotter = agreed_run(tmp_path)

    async with app.run_test() as pilot:
        await reach_the_question(app, pilot)
        await pilot.press("q")
        await wait_for(lambda: app._settled)

        assert not isinstance(app.screen, ConfirmQuitScreen), "q must not kill here"

    assert app.result is not None
    assert app.result.outcome is Outcome.CONSENSUS  # not ABORTED
    assert snapshotter.summary()[0].merged is False
    assert (tmp_path / ".dai" / "runs" / "run1" / "report.md").is_file()


def many_candidates(tmp_path, count):
    return [
        MergeCandidate(
            repo=tmp_path / f"r{i}", label=f"r{i}", branch="dai/run1",
            base_branch="main", added=3, removed=1, files=("a.py", "b.py"),
        )
        for i in range(count)
    ]


def answerable(screen) -> list[str]:
    """The controls the user needs, that the compositor is not actually drawing.

    Deliberately not "is the screen still on the stack" — it always was. What
    the clipping bug took was the bottom of the card, so what has to be asserted
    is that the way to answer is on screen, not that the screen exists.
    """

    visible = screen.app.screen._compositor.visible_widgets
    return [
        name
        for name in ("#merge-go", "#merge-toggle", "#merge-actions")
        if screen.query_one(name) not in visible
    ]


async def test_the_two_controls_are_the_same_width_at_any_size(tmp_path):
    """`auto` sizes each to its own label, and an fr split favours one of them."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        screen = MergeScreen(many_candidates(tmp_path, 3), run_branch="dai/run1")
        app.push_screen(screen)
        await pilot.pause()

        for width in (120, 100, 88, 80, 72, 64):
            await pilot.resize_terminal(width, 34)
            await pilot.pause()
            go = screen.query_one("#merge-go", Button)
            toggle = screen.query_one("#merge-toggle", Cell)
            assert go.region.width == toggle.region.width, f"uneven at {width} columns"


async def test_shrinking_the_terminal_never_takes_away_the_answer(tmp_path):
    """The card is clamped without anything inside giving way, so it clipped —
    and what it clipped was the button, the way out and the footnote."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app)
        # Eight repos: the list is long enough that the card wants more height
        # than a small terminal will give it.
        screen = MergeScreen(many_candidates(tmp_path, 8), run_branch="dai/run1")
        app.push_screen(screen)
        await pilot.pause()

        assert answerable(screen) == []

        for size in ((100, 24), (80, 24), (100, 18), (64, 16), (120, 40)):
            await pilot.resize_terminal(*size)
            for _ in range(3):
                await pilot.pause()
            assert answerable(screen) == [], f"cut off at {size[0]}x{size[1]}"


async def test_a_roomy_terminal_gets_no_bigger_a_card_than_it_needs(tmp_path):
    """The list takes the leftover height; it must not go looking for more."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app)
        screen = MergeScreen(many_candidates(tmp_path, 1), run_branch="dai/run1")
        app.push_screen(screen)
        for _ in range(3):
            await pilot.pause()

        # One repo is two rows of content; the card is its chrome plus that,
        # nowhere near the 90% of the screen it is allowed to take.
        assert screen.query_one("#merge-card").region.height < 24


async def test_merging_without_asking_never_shows_the_screen(tmp_path):
    app, snapshotter = agreed_run(tmp_path, merge=Merge.ALWAYS)
    seen = []

    async with app.run_test() as pilot:
        for _ in range(80):
            seen.append(isinstance(app.screen, MergeScreen))
            if app._settled:
                break
            await asyncio.sleep(0.05)

    assert not any(seen), "told to merge, it asked anyway"
    assert snapshotter.summary()[0].merged is True


# --- layout regression ----------------------------------------------------


async def test_panes_sit_side_by_side_with_status_above_and_verdicts_below(tmp_path):
    """The whole point of the layout: both sides visible at once."""

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test(size=(110, 28)) as pilot:
        await settle(app)
        await pilot.pause()

        status = app.query_one(StatusBar).region
        left, right = (p.region for p in app.query(AgentPane))
        verdicts = app.query_one(VerdictLog).region

        assert status.y == 0
        assert left.x == 0 and right.x == left.width, "panes must not overlap"
        assert left.y == right.y, "panes must be level"
        assert left.width == right.width, "neither side gets more room"
        assert verdicts.y >= left.y + left.height, "verdicts belong below the panes"


async def test_each_round_verdict_reaches_the_log(tmp_path):
    app, _ = make_app(
        tmp_path,
        [solved(), solved(responses=replies(("i1", "FIXED")))],
        [changes("status column empty"), approve()],
    )

    async with app.run_test() as pilot:
        await settle(app)
        await pilot.pause()

        written = " ".join(str(line) for line in app.query_one(VerdictLog).lines)
        assert "REQUEST_CHANGES" in written
        assert "APPROVE" in written
        assert "status column empty" in written


# --- asking for a task ----------------------------------------------------


async def test_task_prompt_returns_what_was_typed(tmp_path):
    from dai.tui.app import TaskPrompt
    from dai.tui.completion import CompletingInput

    app = TaskPrompt(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(CompletingInput).input.value = "fill in the table"
        await pilot.press("enter")
        await pilot.pause()

    assert app.return_value == "fill in the table"


async def test_task_prompt_can_be_cancelled(tmp_path):
    from dai.tui.app import TaskPrompt

    app = TaskPrompt(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()

    assert app.return_value == ""


# --- following the terminal -----------------------------------------------


def styles_of(log) -> list[str]:
    """The style of every visible segment a RichLog has drawn."""

    return [
        str(segment.style)
        for strip in log.lines
        for segment in strip._segments
        if segment.text.strip()
    ]


def styles_in(widget) -> list[str]:
    """The colours a Label or Static baked into its text, normalised to hex.

    Textual writes a style back as `#rrggbb` or as `rgb(r, g, b)` depending on
    how it got there, so the spans are parsed rather than string-matched.
    """

    found = []
    for span in widget.render().spans:
        for token in str(span.style).split():
            try:
                found.append(Color.parse(token).hex.lower())
            except ColorParseError:
                continue
    return found


async def test_the_app_starts_in_the_appearance_it_was_given(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()], appearance="light")

    async with app.run_test():
        assert app.theme == theme.LIGHT_THEME.name
        assert theme.ACTIVE is theme.LIGHT
        assert theme.MUTED == theme.LIGHT.muted


async def test_the_prompt_starts_in_the_appearance_it_was_given(tmp_path):
    from dai.tui.app import TaskPrompt

    app = TaskPrompt(tmp_path, appearance="light")
    async with app.run_test():
        assert app.theme == theme.LIGHT_THEME.name


async def test_a_terminal_that_changes_theme_takes_the_scrollback_with_it(tmp_path):
    """The whole point: what is already on screen has to follow too.

    A RichLog keeps rendered lines, colours and all, so the panes and the score
    are drawn again from what they were made of rather than left in the palette
    they happened to be written in.
    """

    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        pane = app.query_one("#solver", AgentPane)
        pane.handle(AgentEvent("error", "boom"))
        await pilot.pause()

        verdicts = app.query_one(VerdictLog)
        before = styles_of(verdicts), styles_of(pane.log_widget)
        assert theme.DARK.muted in before[0]

        app.post_message(AppearanceChanged("light"))
        await pilot.pause()
        await pilot.pause()

        after = styles_of(verdicts), styles_of(pane.log_widget)

        assert app.theme == theme.LIGHT_THEME.name
        assert theme.ACTIVE is theme.LIGHT
        # Same lines, new colours — not a cleared log, and not a stale one.
        assert [len(side) for side in after] == [len(side) for side in before]
        assert theme.LIGHT.muted in after[0]
        assert theme.DARK.muted not in after[0] + after[1]


async def test_a_theme_change_redraws_the_headings_too(tmp_path):
    app, _ = make_app(tmp_path, [solved()], [approve()])

    async with app.run_test() as pilot:
        await settle(app)
        pane = app.query_one("#solver", AgentPane)
        title = pane.query_one(".pane-title", Label)
        assert theme.DARK.solver in styles_in(title)

        app.post_message(AppearanceChanged("light"))
        await pilot.pause()
        await pilot.pause()

        assert theme.LIGHT.solver in styles_in(title)


async def test_a_pane_survives_being_repainted_before_anything_was_written(tmp_path):
    app = PaneHost(tmp_path)

    async with app.run_test() as pilot:
        pane = app.query_one(AgentPane)
        theme.use("light")
        pane.repaint()
        await pilot.pause()

        assert pane.query_one(".pane-log", RichLog).lines == []
        assert styles_in(pane.query_one(".pane-idle", Static)) == [theme.LIGHT.muted]

    theme.use("dark")


async def test_the_deadlock_screen_is_redrawn_under_a_theme_change(tmp_path):
    """It waits on the user for as long as they take, so it can outlive a palette."""

    app, _ = make_app(tmp_path, [solved()], [approve()])
    issue = Issue(id="i7", severity=Severity.BLOCKER, claim="table still empty",
                  evidence="matrix.md:4")
    pending = DebateResult(
        outcome=Outcome.DEADLOCK, reason="stuck", rounds=[],
        spend=app.debate.budget.spend, open_issues=[issue],
    )

    async with app.run_test() as pilot:
        await settle(app)
        screen = DeadlockScreen(pending, "critic")
        app.push_screen(screen)
        await pilot.pause()

        app.post_message(AppearanceChanged("light"))
        await pilot.pause()
        await pilot.pause()

        rendered = " ".join(str(w.content) for w in screen.query(Static))
        assert "table still empty" in rendered
        assert app.focused is screen.query_one("#critic", Button)


async def test_the_prompt_follows_the_terminal_too(tmp_path):
    from dai.tui.app import TaskPrompt

    app = TaskPrompt(tmp_path)
    async with app.run_test() as pilot:
        title = app.query_one("#prompt-title", Static)
        assert theme.DARK.solver in styles_in(title)

        app.post_message(AppearanceChanged("light"))
        await pilot.pause()
        await pilot.pause()

        assert app.theme == theme.LIGHT_THEME.name
        assert theme.LIGHT.solver in styles_in(title)
        assert theme.LIGHT.muted in styles_in(app.query_one("#prompt-hints", Static))
