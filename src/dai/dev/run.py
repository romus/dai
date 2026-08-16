"""The whole loop, on fake agents, over repositories built for the occasion.

Not a mock of the app: it builds the same `Debate`, `Snapshotter`, `Transcript`
and `DaiApp` that `main()` builds. Only the two engines are fake, and only the
workspace is disposable.

The workspace is the part that is easy to get wrong. With snapshots off the
merge dialog is unreachable — it is only offered for repositories that actually
changed — so a demo that wants to show it needs real git repos that the fake
solver really writes to. One of them has its base branch moved on purpose,
because "the branch moved under the run" is the row state you otherwise cannot
stage on demand.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from dai.budget import Budget, Limits
from dai.config import Merge, SnapshotConfig
from dai.consensus import Referee
from dai.dev.engine import FakeEngine
from dai.dev.scenarios import SCENARIOS, SUBJECT, Scenario
from dai.orchestrator import Debate
from dai.snapshot import Snapshotter, describe, ignore_locally
from dai.transcript import Transcript, new_run_id

#: Where a demo builds its repositories, unless told otherwise with -C.
WORKSPACE = Path("/tmp/dai-demo")

REPOS = ("service", "packages/engine", "vendor/prompts")


def demo(
    *, scenario: str, cwd: Path | None, appearance: str = "dark", no_tui: bool = False
) -> int:
    """Play one canned argument through the real application."""

    picked = SCENARIOS[scenario]
    workspace = (cwd or WORKSPACE).resolve()
    if cwd is None:
        _build_workspace(workspace)
    print(f"demo: {scenario} — {picked.summary}")
    print(f"workspace: {workspace}")

    run_id = new_run_id()
    ignore_locally(workspace, ".dai/")
    settings = SnapshotConfig(merge=Merge.ASK)
    snapshotter = Snapshotter(workspace, run_id, settings)
    transcript = Transcript(workspace, run_id)
    debate = _debate(picked, workspace)

    if no_tui:
        result = asyncio.run(_headless(debate, workspace, transcript, snapshotter))
    else:
        from dai.tui import DaiApp

        app = DaiApp(
            debate, cwd=workspace, transcript=transcript,
            snapshotter=snapshotter, appearance=appearance,
        )
        app.run()
        result = app.result

    if result is None:
        print("demo: ended before reaching a verdict", file=sys.stderr)
        return 1
    print(f"\n{result.outcome.value}: {result.reason}")
    for line in describe(snapshotter.summary(), workspace):
        print(line)
    return 0


def _debate(picked: Scenario, workspace: Path) -> Debate:
    solver, critic = FakeEngine(), FakeEngine()
    solver.name, critic.name = "fake-solver", "fake-critic"
    solver.solves = list(picked.solves)
    critic.critiques = list(picked.critiques)
    solver.cost = critic.cost = picked.cost
    # Every round has to change something, or the repository is never touched
    # and there is nothing to offer at the end.
    solver.writes = {
        f"{name}/{SUBJECT}": "a line the agents added\n" for name in REPOS
    }
    return Debate(
        task="fill in the empty cells of the table, using the README as the source",
        cwd=workspace,
        solver=solver,
        critic=critic,
        budget=Budget(Limits(
            max_rounds=picked.max_rounds, max_usd=picked.max_usd, max_wall_seconds=None
        )),
        referee=Referee(),
        deadlock_policy=picked.policy or "critic",
        solver_writes=True,
    )


async def _headless(debate, cwd, transcript, snapshotter):
    """Enough of the streaming path to see the events without a screen."""

    transcript.start(
        task=debate.task, cwd=cwd, solver=debate.solver.name, critic=debate.critic.name
    )

    def on_event(event):
        if event.kind == "turn_start":
            print(f"  round {event.round} · {event.engine} {event.role.value}")

    async def on_round_start(number):
        if snapshotter.active:
            await asyncio.to_thread(snapshotter.capture_gate, number, debate.last_verdict)

    debate.on_event = on_event
    debate.on_round_start = on_round_start
    result = await debate.run()
    if snapshotter.active:
        await asyncio.to_thread(
            snapshotter.capture_final, f"{result.outcome.value}: {result.reason}"
        )
    transcript.finish(
        result, task=debate.task, cwd=cwd,
        solver=debate.solver.name, critic=debate.critic.name,
        repos=snapshotter.summary(),
    )
    return result


# --- the throwaway repositories -------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _build_workspace(root: Path) -> None:
    """Three small repositories, side by side and one level down.

    Three rather than one because the merge dialog's whole reason for existing
    is that a workspace is not always a repository. `vendor/prompts` is rooted
    on `develop` so the rows do not all merge into the same branch.

    A row the run *cannot* merge is not staged here — it needs the base branch
    to move after the run rooted itself, which is a race to arrange and a
    distraction to maintain. `--preview merge` shows that row instead.
    """

    shutil.rmtree(root, ignore_errors=True)
    for name in REPOS:
        repo = root / name
        repo.mkdir(parents=True)
        (repo / SUBJECT).write_text(
            "# Service matrix\n\n| Service | Port | Status |\n|---|---|---|\n"
            "| auth |  |  |\n| search |  |  |\n",
            encoding="utf-8",
        )
        _git("init", "-q", "-b", "main", cwd=repo)
        _git("config", "user.email", "demo@localhost", cwd=repo)
        _git("config", "user.name", "demo", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "initial", cwd=repo)
    _git("checkout", "-q", "-b", "develop", cwd=root / REPOS[-1])
