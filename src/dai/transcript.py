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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dai.models import Outcome
from dai.orchestrator import DebateResult, Round
from dai.snapshot import RepoResult, SnapshotReport, addressed

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

    def snapshots(self, report: SnapshotReport) -> None:
        if not report.taken and not report.skipped:
            return
        self.event(
            "snapshot",
            label=report.label,
            commits=[
                {"repo": str(s.repo), "branch": s.branch, "commit": s.commit}
                for s in report.taken
            ],
            skipped=report.skipped,
        )

    def branches(self, repos: Sequence[RepoResult]) -> None:
        """Where the run's commits ended up, once, at the end.

        `snapshots()` says nothing for a round that changed nothing, which for a
        whole run that changed nothing leaves no trace of the branches at all.
        This is the record that always exists.
        """

        if not repos:
            return
        self.event(
            "branches",
            repos=[
                {"repo": str(r.repo), "branch": r.branch, "base": r.base,
                 "commits": r.commits, "merged": r.merged, "note": r.note}
                for r in repos
            ],
        )

    def finish(self, result: DebateResult, *, task: str, cwd: Path,
               solver: str, critic: str,
               repos: Sequence[RepoResult] = ()) -> Path | None:
        self.branches(repos)
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
            result, run_id=self.run_id, task=task, cwd=cwd, solver=solver,
            critic=critic, repos=repos,
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
    result: DebateResult, *, run_id: str, task: str, cwd: Path, solver: str,
    critic: str, repos: Sequence[RepoResult] = (),
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

    lines += _render_work(repos, cwd)
    return "\n".join(lines)


def _render_work(repos: Sequence[RepoResult], cwd: Path) -> list[str]:
    """Where the work is, per repository, in commands that actually run.

    Naming the branch and nothing else was the old bug: the directory a run
    starts in need not be a repository, so every `git log HEAD..<branch>` typed
    there answers "not a git repository" — which reads exactly like the run
    having committed nothing.
    """

    # No repositories at all means nothing was committed — a dry run, or
    # snapshots switched off. Naming a branch that does not exist would be
    # worse than saying nothing.
    if not repos:
        return []

    touched = [entry for entry in repos if entry.touched]
    idle = [entry for entry in repos if not entry.touched]
    lines = ["## The work, round by round", ""]

    if not touched:
        lines += [
            "Nothing on disk changed, so nothing was committed. The branch "
            f"`{repos[0].branch}` exists in {_count(repos, 'repository', 'repositories')}",
            "anyway, pointing at the commit you started from — there is simply nothing",
            "on it yet. Delete it with `git branch -D`.",
            "",
        ]
        return lines

    total = sum(entry.commits for entry in touched)
    where = (
        f"{len(touched)} of {_count(repos, 'repository', 'repositories')}"
        if idle
        else _count(touched, "repository", "repositories")
    )
    lines += [
        f"{_count(total, 'commit', 'commits')} in {where}, on a branch rooted where you",
        "started. Capturing them moved nothing: not the branch you are on, not HEAD,",
        "not the index, not the working tree.",
        "",
    ]

    for entry in touched:
        at = addressed(entry.repo, cwd)
        base = entry.base[:12]
        lines += [
            f"### {_name(entry.repo, cwd)} — `{entry.branch}`, "
            f"{_count(entry.commits, 'commit', 'commits')}",
            "",
            "```",
            f"{at} log --oneline {f'{base}..' if base else ''}{entry.branch}",
        ]
        if base:
            lines.append(f"{at} diff {base} {entry.branch}")

        if entry.merged:
            # Offering `reset --hard <branch>` again would be a no-op; the undo
            # is the only thing standing between the user and their reflog.
            lines += [
                f"{at} reset --hard {base}   # undo the merge",
                "```",
                "",
                "Your branch was moved onto this work — it is already yours.",
                "",
            ]
            continue

        lines += [f"{at} reset --hard {entry.branch}", "```", ""]
        if entry.note:
            lines += [f"Not merged: {entry.note}.", ""]

    if any(not entry.merged for entry in touched):
        lines += [
            "The working tree already holds the last commit, which is why `reset --hard`",
            "is the way to keep it: it moves your branch onto the work and rewrites no",
            "file. (`git merge --ff-only` refuses — from git's side those are uncommitted",
            "changes it would be overwriting.)",
            "",
        ]
    lines += ["To throw a run away instead, delete its branch with `git branch -D`.", ""]

    if idle:
        lines += [
            "Unchanged, branch left pointing where you started: "
            + ", ".join(f"`{entry.repo.name}`" for entry in idle)
            + ".",
            "",
        ]
    return lines


def _name(repo: Path, cwd: Path) -> str:
    try:
        return str(repo.relative_to(cwd)) if repo != cwd else repo.name
    except ValueError:
        return str(repo)


def _count(value: object, one: str, many: str) -> str:
    n = value if isinstance(value, int) else len(value)  # type: ignore[arg-type]
    return f"{n} {one if n == 1 else many}"


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
