"""Recording a run: a machine-readable event log and a report you can read.

The argument is the interesting artefact, not just its outcome. When the two
agents disagree about something real, the exchange is what tells you which of
them was right — so it is written down as it happens, not reconstructed at the
end from whatever survived in memory.
"""

from __future__ import annotations

import json
import os
import random
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dai.models import Outcome
from dai.orchestrator import DebateResult, Round
from dai.snapshot import SnapshotReport

RUNS_DIR = ".dai/runs"


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{stamp}-{suffix}"


@dataclass
class RunInfo:
    run_id: str
    path: Path
    task: str = ""
    outcome: str = ""
    started: str = ""


class Transcript:
    """Append-only record of one run."""

    def __init__(self, root: Path, run_id: str, *, enabled: bool = True) -> None:
        self.run_id = run_id
        self.enabled = enabled
        self.dir = Path(root) / RUNS_DIR / run_id
        self._events = self.dir / "events.jsonl"
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                # A read-only workspace must not stop the argument.
                self.enabled = False

    # --- writing ----------------------------------------------------------

    def event(self, kind: str, **data) -> None:
        if not self.enabled:
            return
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
        try:
            with self._events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            self.enabled = False

    def start(self, *, task: str, cwd: Path, solver: str, critic: str) -> None:
        self.event(
            "start", task=task, cwd=str(cwd), solver=solver, critic=critic,
            pid=os.getpid(),
        )

    def snapshots(self, label: str, report: SnapshotReport) -> None:
        if not report.taken and not report.skipped:
            return
        self.event(
            "snapshot",
            label=label,
            refs=[{"repo": str(s.repo), "ref": s.ref, "commit": s.commit} for s in report.taken],
            skipped=report.skipped,
        )

    def finish(self, result: DebateResult, *, task: str, cwd: Path,
               solver: str, critic: str) -> Path | None:
        self.event(
            "finish",
            outcome=result.outcome.value,
            reason=result.reason,
            rounds=len(result.rounds),
            turns=result.spend.turns,
            tokens=result.spend.tokens,
            usd=round(result.spend.usd, 4),
            exact_cost=result.spend.exact,
        )
        if not self.enabled:
            return None
        report = render_report(
            result, run_id=self.run_id, task=task, cwd=cwd, solver=solver, critic=critic
        )
        target = self.dir / "report.md"
        try:
            target.write_text(report, encoding="utf-8")
        except OSError:
            return None
        return target


# --- reading --------------------------------------------------------------


def list_runs(root: Path, limit: int = 20) -> list[RunInfo]:
    base = Path(root) / RUNS_DIR
    if not base.is_dir():
        return []

    runs = []
    for directory in sorted(base.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        info = RunInfo(run_id=directory.name, path=directory)
        for record in read_events(directory):
            if record.get("kind") == "start":
                info.task = record.get("task", "")
                info.started = record.get("ts", "")
            elif record.get("kind") == "finish":
                info.outcome = record.get("outcome", "")
        runs.append(info)
        if len(runs) >= limit:
            break
    return runs


def read_events(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "events.jsonl"
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# --- report ---------------------------------------------------------------

_HEADLINE = {
    Outcome.CONSENSUS: "Agreed",
    Outcome.DEADLOCK: "Deadlocked",
    Outcome.BUDGET: "Out of budget",
    Outcome.ROUNDS: "Out of rounds",
    Outcome.ABORTED: "Stopped",
    Outcome.FAILED: "Failed",
}


def render_report(
    result: DebateResult, *, run_id: str, task: str, cwd: Path, solver: str, critic: str
) -> str:
    spend = result.spend
    money = f"${spend.usd:.2f}"
    if not spend.exact:
        money += f" plus unmeasured spend by {', '.join(sorted(spend.unpriced))}"

    lines = [
        f"# dai run {run_id}",
        "",
        f"**Task:** {task}",
        "",
        f"- **Outcome:** {_HEADLINE[result.outcome]} — {result.reason}",
        f"- **Solver:** {solver} · **Critic:** {critic}",
        f"- **Directory:** `{cwd}`",
        f"- **Spend:** {spend.turns} turns · {spend.tokens:,} tokens · {money}",
        "",
    ]

    for rnd in result.rounds:
        lines += _render_round(rnd)

    if result.open_issues:
        lines += ["## Left unresolved", ""]
        for issue in result.open_issues:
            lines.append(f"- **[{issue.id}]** ({issue.severity.value}) {issue.claim}")
            if issue.evidence:
                lines.append(f"  - evidence: {issue.evidence}")
        lines.append("")

    # Name refs that actually exist: a single-round run has no r2 to diff against.
    first = "r1"
    last = f"r{len(result.rounds)}" if len(result.rounds) > 1 else "final"

    lines += [
        "## Recovering a round",
        "",
        "Each round was snapshotted without touching your branch, HEAD, index or",
        "working tree:",
        "",
        "```",
        f"git diff refs/dai/{run_id}/{first} refs/dai/{run_id}/{last}",
        f"git restore --source refs/dai/{run_id}/{first} -- .",
        "```",
        "",
    ]
    return "\n".join(lines)


def _render_round(rnd: Round) -> list[str]:
    lines = [f"## Round {rnd.number}", ""]

    if rnd.solver is not None:
        lines += ["### Solver", "", rnd.solver.summary or "_(no summary)_", ""]
        if rnd.solver.files_changed:
            lines.append("Files changed: " + ", ".join(f"`{f}`" for f in rnd.solver.files_changed))
            lines.append("")
        if rnd.solver.replies:
            lines += ["| Issue | Answer | Detail |", "|---|---|---|"]
            for reply in rnd.solver.replies:
                detail = reply.detail.replace("|", "\\|")
                lines.append(f"| {reply.id} | {reply.action.value} | {detail} |")
            lines.append("")

    if rnd.critic is not None:
        lines += [f"### Critic — {rnd.critic.verdict.value}", ""]
        if rnd.critic.summary:
            lines += [rnd.critic.summary, ""]
        if rnd.critic.checked:
            lines.append("Checked:")
            lines += [f"- {item}" for item in rnd.critic.checked]
            lines.append("")
        if rnd.critic.conceded:
            lines += [f"Conceded: {', '.join(rnd.critic.conceded)}", ""]
        for issue in rnd.critic.open_issues:
            lines.append(f"- **[{issue.id}]** ({issue.severity.value}) {issue.claim}")
            if issue.evidence:
                lines.append(f"  - evidence: {issue.evidence}")
            if issue.fix:
                lines.append(f"  - suggested: {issue.fix}")
        if rnd.critic.open_issues:
            lines.append("")

    if rnd.notes:
        lines += ["> " + note for note in rnd.notes]
        lines.append("")

    return lines
