"""A run you can watch and press buttons in, that costs and changes nothing.

`dai --demo` plays a canned argument through the real application — the real
`Debate`, the real screen, the real merge dialog — with two differences: the
engines are fabrications rather than agent CLIs, so no tokens are spent and
neither `claude` nor `codex` needs to be installed, and the snapshotter is a
fabrication too, so **not one byte is written anywhere**. No files, no git, no
transcript, no sandbox.

That last part is what makes it safe to point a brand-new user at, which is the
whole reason it ships.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dai.budget import Budget, Limits
from dai.consensus import Referee
from dai.demo.engine import FakeEngine
from dai.demo.fiction import PretendSnapshotter
from dai.demo.script import CRITIQUES, SOLVES, TASK
from dai.orchestrator import Debate
from dai.transcript import Transcript
# Textual, at module level: `__main__` imports this package only when --demo is
# given, so the headless path still never pays for it.
from dai.tui import app as tui_app
from dai.tui.app import DaiApp
from dai.tui.widgets import VerdictLog

__all__ = ["run", "FakeEngine", "PretendSnapshotter"]


def run(cwd: Path, *, appearance: str = "dark") -> int:
    """Play the demo. Returns an exit code, like every other mode."""

    if not sys.stdout.isatty():
        print(
            "dai: --demo needs a terminal to draw on; run it without a pipe",
            file=sys.stderr,
        )
        return 2

    snapshotter = PretendSnapshotter(cwd)
    app = _DemoApp(
        _debate(cwd),
        cwd=cwd,
        # Disabled, not redirected: the run must leave nothing in `~/.dai` either.
        transcript=Transcript(cwd, "demo", enabled=False),
        snapshotter=snapshotter,
        appearance=appearance,
    )
    app.run()

    # Said out here, after Textual has given the terminal back: everything the
    # demo drew is gone with the alternate screen, and this is the one line
    # that survives to correct any impression the fiction left behind.
    print(_closing(snapshotter))
    return 0


class _DemoApp(DaiApp):
    """The real screen, which says so before it starts pretending.

    The banner it inherits announces a branch and a commit per round, and the
    ledger at the end says which repository you are standing on. Both are true
    of a real run and neither is true here, so the fiction declares itself in
    the one place the user is already reading.
    """

    #: Spelled out because Textual resolves a relative `CSS_PATH` against the
    #: module of the class that declares it — inherit it and this subclass goes
    #: looking for a stylesheet next to itself, which is not where it lives.
    CSS_PATH = Path(tui_app.__file__).with_name("styles.tcss")

    def on_mount(self) -> None:
        # Deliberately no `super().on_mount()`: Textual dispatches a handler to
        # every class in the MRO that defines one, subclass first. Calling up
        # as well runs the base twice — which here means two debate workers on
        # one script, and the second one starving.
        self.query_one(VerdictLog).note(
            "demo — the agents, the repositories and the commits below are all "
            "invented, and nothing is written anywhere",
            style="strong",
        )


def _debate(cwd: Path) -> Debate:
    solver, critic = FakeEngine(), FakeEngine()
    solver.name, critic.name = "solver (demo)", "critic (demo)"
    solver.solves = list(SOLVES)
    critic.critiques = list(CRITIQUES)
    return Debate(
        task=TASK,
        cwd=cwd,
        solver=solver,
        critic=critic,
        budget=Budget(Limits(max_rounds=5, max_usd=5.0, max_wall_seconds=None)),
        referee=Referee(),
        # The whole first act: they stall, you rule, and the run carries on to
        # agreement — which is what makes the merge dialog reachable after.
        deadlock_policy="ask",
        solver_writes=False,
    )


def _closing(snapshotter: PretendSnapshotter) -> str:
    what = (
        f"you chose to merge {len(snapshotter.chosen)} of 3"
        if snapshotter.chosen
        else "you kept the branches"
    )
    return (
        f"that was a demo — {what}, but nothing was written and no agent ran.\n"
        'for the real thing: dai "your task here"'
    )
