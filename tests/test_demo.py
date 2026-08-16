"""`dai --demo`: it must show the whole run, and leave no trace of it."""

from __future__ import annotations

import asyncio
import glob
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from dai import demo
from dai.__main__ import build_parser
from dai.demo.engine import FakeEngine
from dai.demo.fiction import BRANCH, PretendSnapshotter, candidates
from dai.demo.script import CRITIQUES, SOLVES
from dai.models import Access, Outcome
from dai.transcript import Transcript
from dai.tui.app import DeadlockScreen, MergeScreen
from dai.tui.widgets import RepoRow, VerdictLog

ROOT = Path(__file__).resolve().parent.parent


def demo_app(cwd: Path) -> tuple[DaiApp, PretendSnapshotter]:
    """The demo exactly as `--demo` builds it, minus the pacing."""

    snapshotter = PretendSnapshotter(cwd)
    debate = demo._debate(cwd)
    debate.solver.beat = debate.critic.beat = 0
    app = demo._DemoApp(
        debate, cwd=cwd, transcript=Transcript(cwd, "demo", enabled=False),
        snapshotter=snapshotter, appearance="dark",
    )
    return app, snapshotter


async def reach(app, screen, tries: int = 300):
    for _ in range(tries):
        if isinstance(app.screen, screen):
            return app.screen
        await asyncio.sleep(0.05)
    raise AssertionError(f"the demo never reached {screen.__name__}")


async def rule_the_deadlock(app, pilot, side: str = "left"):
    """Settle every open issue, then continue — the demo's first act."""

    screen = await reach(app, DeadlockScreen)
    for _ in range(len(screen.cases)):
        await pilot.press(side)
    await pilot.press("enter")
    return screen


async def reach_the_merge(app, tries: int = 300):
    return await reach(app, MergeScreen, tries)


async def settled(app, tries: int = 120) -> None:
    for _ in range(tries):
        if app._settled:
            return
        await asyncio.sleep(0.05)


# --- the promise -----------------------------------------------------------


async def test_the_demo_writes_absolutely_nothing(tmp_path):
    """The whole reason it is safe to point a stranger at.

    Not "no agents ran" — nothing at all: no files, no `.dai/`, no repository.
    A sandbox quietly reintroduced anywhere would fail here.
    """

    app, _ = demo_app(tmp_path)

    async with app.run_test() as pilot:
        await rule_the_deadlock(app, pilot)
        await reach_the_merge(app)
        await pilot.press("enter")  # the destructive answer, on purpose
        await settled(app)

    assert list(tmp_path.iterdir()) == [], "the demo left something behind"


async def test_the_fake_engine_writes_nothing_even_when_told_it_may(tmp_path):
    engine = FakeEngine()
    engine.beat = 0
    engine.solves = list(SOLVES)

    await engine.run("do it", cwd=tmp_path, access=Access.WRITE)

    assert list(tmp_path.iterdir()) == []


def test_the_fake_engine_survives_the_check_that_runs_before_the_tui():
    """/bin/true, which the test double uses, does not exist on macOS."""

    assert shutil.which(FakeEngine().cmd) is not None


# --- and it really is the application --------------------------------------


async def test_the_demo_reaches_the_merge_dialog(tmp_path):
    app, snapshotter = demo_app(tmp_path)

    async with app.run_test() as pilot:
        await rule_the_deadlock(app, pilot)
        screen = await reach_the_merge(app)
        rows = [row.candidate for row in screen.query(RepoRow)]

        assert len(rows) == 3
        assert any(not row.mergeable for row in rows), "no refused row to look at"
        assert all(row.branch == BRANCH for row in rows)

        await pilot.press("escape")
        await settled(app)

    assert app.result is not None and app.result.outcome is Outcome.CONSENSUS
    assert snapshotter.chosen == ()


async def test_choosing_to_merge_runs_the_real_path_over_the_fiction(tmp_path):
    """Enter takes the same route a real run takes; it just moves nothing."""

    app, snapshotter = demo_app(tmp_path)

    async with app.run_test() as pilot:
        await rule_the_deadlock(app, pilot)
        await reach_the_merge(app)
        await pilot.press("enter")
        await settled(app)

    # Two of the three can go; the third said why it could not.
    assert len(snapshotter.chosen) == 2
    merged = {entry.repo: entry.merged for entry in snapshotter.summary()}
    assert sum(merged.values()) == 2
    assert list(tmp_path.iterdir()) == []


async def test_the_screen_says_it_is_a_demo_before_it_pretends(tmp_path):
    """The banner it inherits promises commits; none of them will happen."""

    app, _ = demo_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        said = "\n".join(str(entry.text) for entry in app.query_one(VerdictLog)._entries)

        assert "invented" in said and "nothing is written" in said


def test_the_ledger_prints_no_commit_range_it_cannot_back_up(tmp_path):
    """An invented sha would read as one you could go and look up."""

    from dai.snapshot import describe

    lines = "\n".join(describe(PretendSnapshotter(tmp_path).summary(), tmp_path))

    assert ".." not in lines


def test_the_fiction_offers_a_row_that_cannot_be_merged(tmp_path):
    rows = candidates(tmp_path)

    assert [row.mergeable for row in rows].count(False) == 1
    assert all(row.refusal or row.files for row in rows)


def test_the_closing_note_never_claims_something_was_written(tmp_path):
    snapshotter = PretendSnapshotter(tmp_path)
    kept = demo._closing(snapshotter)
    snapshotter.merge(only=[candidates(tmp_path)[0].repo])
    merged = demo._closing(snapshotter)

    for note in (kept, merged):
        assert "nothing was written" in note and "no agent ran" in note
    assert "kept the branches" in kept
    assert "merge 1 of 3" in merged


def test_the_script_agrees_without_being_challenged():
    """An approval that names none of the changed files costs the run a turn."""

    approval = CRITIQUES[-1]

    assert approval["verdict"] == "APPROVE"
    assert approval["checked"], "an approval that checked nothing is rejected"
    changed = SOLVES[-1]["files_changed"][0]
    assert any(changed in line for line in approval["checked"])


# --- it ships --------------------------------------------------------------


def test_demo_is_an_ordinary_flag():
    """It is a feature now, not something bolted on when a module happens to be there."""

    args = build_parser().parse_args(["--demo"])

    assert args.demo is True
    assert build_parser().parse_args(["a task"]).demo is False


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build a wheel")
def test_the_demo_is_in_the_built_wheel():
    with tempfile.TemporaryDirectory() as out:
        built = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", out],
            cwd=ROOT, capture_output=True, text=True,
        )
        assert built.returncode == 0, built.stderr
        names = zipfile.ZipFile(sorted(glob.glob(f"{out}/*.whl"))[-1]).namelist()

    # Guards against passing on a wheel that simply has nothing in it.
    assert "dai/tui/app.py" in names
    assert "dai/demo/__init__.py" in names
    assert "dai/demo/fiction.py" in names
