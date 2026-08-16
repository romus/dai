"""The developer tools: they must work, and they must not ship."""

from __future__ import annotations

import glob
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("dai.dev", reason="dev tools are stripped from a released build")

from dai.budget import Budget, Limits
from dai.consensus import Referee
from dai.dev.engine import FakeEngine
from dai.dev.scenarios import SCENARIOS
from dai.dev.screens import NAMES, _Host
from dai.engines import ENGINES, build_engine
from dai.models import Outcome
from dai.orchestrator import Debate
from dai.tui.app import ConfirmQuitScreen, DeadlockScreen, InjectScreen, MergeScreen

ROOT = Path(__file__).resolve().parent.parent


def played(scenario: str, tmp_path: Path):
    """Run one canned argument through a real Debate."""

    picked = SCENARIOS[scenario]
    solver, critic = FakeEngine(), FakeEngine()
    solver.beat = critic.beat = 0  # the pacing is for eyes, not for tests
    solver.solves = list(picked.solves)
    critic.critiques = list(picked.critiques)
    solver.cost = critic.cost = picked.cost
    debate = Debate(
        task="fill in the table",
        cwd=tmp_path,
        solver=solver,
        critic=critic,
        budget=Budget(Limits(
            max_rounds=picked.max_rounds, max_usd=picked.max_usd, max_wall_seconds=None
        )),
        referee=Referee(),
        deadlock_policy=picked.policy or "critic",
    )
    return debate


# --- the fake engine -------------------------------------------------------


def test_the_fake_engine_survives_the_check_that_runs_before_the_tui():
    """`_missing_binaries` rejects an engine whose cmd is not on PATH.

    /bin/true, which the test double uses, does not exist on macOS — it only
    gets away with it because tests never reach that check.
    """

    assert shutil.which(FakeEngine().cmd) is not None


def test_the_fake_engine_is_selectable_by_name():
    """Importing the dev tools is the whole registration."""

    assert "fake" in ENGINES
    assert isinstance(build_engine("fake"), FakeEngine)


def test_one_engine_plays_both_seats(tmp_path):
    """Solver or critic is decided by the schema it was handed, not by order."""

    from dai.protocol import CRITIC_SCHEMA, SOLVER_SCHEMA

    assert FakeEngine._is_critic_schema(CRITIC_SCHEMA)
    assert not FakeEngine._is_critic_schema(SOLVER_SCHEMA)
    assert not FakeEngine._is_critic_schema(None)


async def test_a_solve_turn_leaves_something_behind(tmp_path):
    """Without a real change nothing is committed and no merge is ever offered."""

    engine = FakeEngine()
    engine.beat = 0
    engine.solves = [SCENARIOS["quick"].solves[0]]
    engine.writes = {"notes.md": "a line\n"}

    from dai.models import Access

    await engine.run("do it", cwd=tmp_path, access=Access.WRITE)

    assert (tmp_path / "notes.md").read_text() == "a line\n"


async def test_a_critic_turn_writes_nothing(tmp_path):
    engine = FakeEngine()
    engine.beat = 0
    engine.critiques = [SCENARIOS["quick"].critiques[0]]
    engine.writes = {"notes.md": "a line\n"}

    from dai.models import Access
    from dai.protocol import CRITIC_SCHEMA

    await engine.run("review", cwd=tmp_path, access=Access.READ_ONLY, schema=CRITIC_SCHEMA)

    assert list(tmp_path.iterdir()) == []


# --- the scenarios reach the endings they exist to show --------------------


@pytest.mark.parametrize(
    "scenario, outcome",
    [
        ("agree", Outcome.CONSENSUS),
        ("quick", Outcome.CONSENSUS),
        ("rubber-stamp", Outcome.CONSENSUS),
        ("deadlock", Outcome.DEADLOCK),
        ("rounds", Outcome.ROUNDS),
        ("budget", Outcome.BUDGET),
    ],
)
async def test_each_scenario_ends_the_way_it_advertises(scenario, outcome, tmp_path):
    """A scenario that stops reaching its ending stops showing its screen."""

    result = await played(scenario, tmp_path).run()

    assert result.outcome is outcome, f"{scenario}: {result.reason}"


async def test_the_asking_scenario_actually_asks(tmp_path):
    """`deadlock-ask` exists to put DeadlockScreen up; nothing else does."""

    debate = played("deadlock-ask", tmp_path)
    asked = []

    async def on_deadlock(pending):
        asked.append(pending)
        return "solver"

    debate.on_deadlock = on_deadlock
    result = await debate.run()

    assert asked, "the run resolved itself instead of asking"
    assert "solver's version stands" in result.reason


async def test_a_scenario_that_runs_dry_says_so_rather_than_hanging(tmp_path):
    engine = FakeEngine()
    engine.beat = 0

    result = await engine.run("go")

    assert result.error and "nothing left in the script" in result.error


# --- the previews ----------------------------------------------------------


@pytest.mark.parametrize(
    "name, screen",
    [
        ("merge", MergeScreen),
        ("deadlock", DeadlockScreen),
        ("inject", InjectScreen),
        ("quit", ConfirmQuitScreen),
    ],
)
async def test_every_preview_mounts_the_screen_it_names(name, screen, tmp_path):
    """The test that stops a preview rotting when a constructor changes."""

    app = _Host(name, tmp_path, "dark")

    async with app.run_test() as pilot:
        await pilot.pause()

        assert isinstance(app.screen, screen)


async def test_the_merge_preview_shows_a_row_that_cannot_go(tmp_path):
    """The refused row is the state a real demo cannot stage on demand."""

    from dai.tui.widgets import RepoRow

    app = _Host("merge", tmp_path, "dark")

    async with app.run_test() as pilot:
        await pilot.pause()
        rows = [row.candidate for row in app.screen.query(RepoRow)]

        assert len(rows) > 1
        assert any(not row.mergeable for row in rows)
        assert any(row.mergeable for row in rows)


def test_every_advertised_preview_name_can_be_built():
    """`task` is a whole App rather than a modal, so it is the one exception."""

    assert set(NAMES) - {"task"} == {"merge", "deadlock", "inject", "quit"}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_the_demo_really_reaches_the_merge_dialog(tmp_path):
    """The reason the demo builds real repositories instead of faking them.

    With nothing committed there is nothing to offer, and the dialog this whole
    mode exists to look at never opens.
    """

    import asyncio

    from dai.config import Merge, SnapshotConfig
    from dai.dev.run import _build_workspace, _debate
    from dai.snapshot import Snapshotter
    from dai.transcript import Transcript
    from dai.tui.app import DaiApp

    workspace = tmp_path / "demo"
    _build_workspace(workspace)
    debate = _debate(SCENARIOS["quick"], workspace)
    debate.solver.beat = debate.critic.beat = 0
    snapshotter = Snapshotter(workspace, "run1", SnapshotConfig(merge=Merge.ASK))
    app = DaiApp(
        debate, cwd=workspace, transcript=Transcript(workspace, "run1"),
        snapshotter=snapshotter, appearance="dark",
    )

    async with app.run_test() as pilot:
        for _ in range(120):
            if isinstance(app.screen, MergeScreen):
                break
            await asyncio.sleep(0.05)

        assert isinstance(app.screen, MergeScreen), "the demo never offered a merge"
        assert len(snapshotter.summary()) == 3, "all three repositories should have moved"

        await pilot.press("escape")
        for _ in range(80):
            if app._settled:
                break
            await asyncio.sleep(0.05)

    assert app.result is not None and app.result.agreed


# --- and none of it ships --------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build a wheel")
def test_the_dev_tools_are_not_in_the_built_wheel():
    """The whole point of the feature, asserted against a real build."""

    with tempfile.TemporaryDirectory() as out:
        built = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", out],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert built.returncode == 0, built.stderr
        wheel = sorted(glob.glob(f"{out}/*.whl"))[-1]
        names = zipfile.ZipFile(wheel).namelist()

    # Without this the test would pass on an empty wheel, which proves nothing.
    assert "dai/tui/app.py" in names, "the wheel is missing the app itself"
    assert [n for n in names if n.startswith("dai/dev/")] == []
