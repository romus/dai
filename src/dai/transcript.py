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
import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dai import home
from dai.models import Outcome
from dai.orchestrator import DebateResult, Round
from dai.snapshot import RepoResult, SnapshotReport, addressed

STAMP = "%Y%m%d-%H%M%S"

#: What a run id may look like when somebody types one. Anything else — a
#: slash, `..` — would let `--show` wander out of the runs directory.
_RUN_ID = re.compile(r"[A-Za-z0-9._-]+")


def new_run_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime(STAMP)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{stamp}-{suffix}"


def runs_root(workdir: Path) -> Path:
    """Every run made in one working directory — under `~/.dai`, not in it."""

    return home.project_dir(workdir) / "runs"


def run_dir(workdir: Path, run_id: str) -> Path:
    """Where one run keeps its own files.

    Not only the transcript's business any more: a screenshot pasted into the
    prompt lands here too, and it lands before `Transcript` exists.
    """

    return runs_root(workdir) / run_id


@dataclass
class RunInfo:
    run_id: str
    path: Path
    task: str = ""
    outcome: str = ""
    started: str = ""
    #: The directory the run worked in, as the run itself recorded it.
    cwd: str = ""
    pid: int | None = None

    @property
    def finished(self) -> bool:
        return bool(self.outcome)


class Transcript:
    """Append-only record of one run."""

    def __init__(self, workdir: Path, run_id: str, *, enabled: bool = True) -> None:
        self.run_id = run_id
        self.enabled = enabled
        self.dir = run_dir(workdir, run_id)
        self._events = self.dir / "events.jsonl"
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                # An unwritable home must not stop the argument.
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


def list_runs(workdir: Path, limit: int = 20) -> list[RunInfo]:
    """The runs made in one working directory, newest first."""

    return _newest(_children(runs_root(workdir)), limit)


def list_all_runs(limit: int = 20) -> list[RunInfo]:
    """The runs made anywhere, newest first."""

    return _newest(_every_run_dir(), limit)


def read_run(directory: Path) -> RunInfo | None:
    """What a run's own log says about it, or None if it is not a run.

    A directory with no events is not a run: it is what is left when a
    screenshot was pasted into a prompt the user then backed out of, or when a
    run died before it said anything.
    """

    if not (directory / "events.jsonl").is_file():
        return None
    info = RunInfo(run_id=directory.name, path=directory)
    for record in read_events(directory):
        if record.get("kind") == "start":
            info.task = record.get("task", "")
            info.started = record.get("ts", "")
            info.cwd = record.get("cwd", "")
            pid = record.get("pid")
            info.pid = pid if isinstance(pid, int) else None
        elif record.get("kind") == "finish":
            info.outcome = record.get("outcome", "")
    return info


def find_run(run_id: str, workdir: Path) -> Path | None:
    """A run's directory, looked for here first and then in every project.

    Run ids are unique on their own, so `--show` needs no `-C`: the id you were
    shown anywhere is enough.
    """

    if not _RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        return None
    here = run_dir(workdir, run_id)
    if here.is_dir():
        return here
    projects = home.projects_dir()
    if not projects.is_dir():
        return None
    found = sorted(projects.glob(f"*/runs/{run_id}"))
    return found[0] if found else None


def run_started(run_id: str, directory: Path) -> datetime:
    """When a run began: its id says so, or failing that its directory's mtime."""

    try:
        return datetime.strptime(run_id[:15], STAMP)
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(directory.stat().st_mtime)
    except OSError:
        return datetime.now()


def pid_alive(pid: object) -> bool:
    """Is the process that started a run still there?

    Anything but a positive int is "no" before it gets near `os.kill`: pid 0
    signals our own process group, and -1 every process we may signal. When the
    answer is unclear — the process exists but is somebody else's — it is
    "yes", because the caller is deciding whether it may delete something.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _children(base: Path) -> list[Path]:
    try:
        return [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return []


def _every_run_dir() -> list[Path]:
    return [d for project in _children(home.projects_dir())
            for d in _children(project / "runs")]


def _newest(directories: list[Path], limit: int) -> list[RunInfo]:
    runs = []
    # By name, which is by time: the id starts with a timestamp.
    for directory in sorted(directories, key=lambda d: d.name, reverse=True):
        info = read_run(directory)
        if info is None:
            continue
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

    # No repositories means none of them changed — or a dry run, or snapshots
    # switched off. Naming a branch that does not exist is worse than silence.
    if not repos:
        return []

    lines = ["## The work, round by round", ""]
    total = sum(entry.commits for entry in repos)
    lines += [
        f"{_count(total, 'commit', 'commits')} in "
        f"{_count(repos, 'repository', 'repositories')}. Repositories nothing changed",
        "in were not touched at all — no branch, no commit, no switch.",
        "",
    ]

    for entry in repos:
        at = addressed(entry.repo, cwd)
        base = entry.base[:12]
        lines += [
            f"### {_name(entry.repo, cwd)} — `{entry.standing_on}`, "
            f"{_count(entry.commits, 'commit', 'commits')}",
            "",
        ]
        if entry.merged:
            lines += [
                f"Merged into `{entry.base_branch}`, and that is the branch you are on "
                "now — the",
                "work is committed, and `git status` is clean.",
                "",
                "```",
                f"{at} log --oneline {f'{base}..' if base else ''}{entry.base_branch}",
            ]
            if base:
                lines += [
                    f"{at} diff {base} {entry.base_branch}",
                    f"{at} reset --hard {base}   # undo, putting {entry.base_branch} back",
                ]
            lines += ["```", ""]
            continue

        lines += [
            f"You are on `{entry.branch}`, with the work committed there.",
            "",
            "```",
            f"{at} log --oneline {f'{base}..' if base else ''}{entry.branch}",
        ]
        if base:
            lines.append(f"{at} diff {base} {entry.branch}")
        if entry.base_branch:
            lines.append(
                f"{at} checkout {entry.base_branch}   # back where you started"
            )
        lines += ["```", ""]
        if entry.note:
            lines += [
                f"Not merged into `{entry.base_branch}`: {entry.note}. Nothing is lost —",
                "the work is committed on the branch above, yours to merge by hand.",
                "",
            ]

    lines += ["To throw a run away, delete its branch with `git branch -D`.", ""]
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
