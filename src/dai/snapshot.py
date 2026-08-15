"""Per-round commits on a branch of dai's own, leaving your branch alone.

The obvious implementation — `git add -A && git commit` — is unacceptable: it
moves HEAD, rewrites the branch you are on and stages your own work-in-progress.

Instead each round is built in a throwaway index (`GIT_INDEX_FILE`), turned into
a commit with `commit-tree`, and chained onto a branch of dai's own,
`dai/<run-id>`. Nothing observable changes: not HEAD, not your branch, not the
index, not the working tree. `git commit` is never run either, so your
pre-commit hooks cannot rewrite files under the agents' feet.

What you gain is an ordinary branch with an ordinary linear history — one commit
per round, rooted at the HEAD you started from, ready for `git log`, `git diff`,
`git merge --ff-only` and `git push` like anything else.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dai.config import SnapshotConfig

#: Directories never worth walking into when looking for repositories.
ALWAYS_SKIP = {".git", "__pycache__"}


@dataclass(frozen=True)
class Snapshot:
    repo: Path
    branch: str
    commit: str
    label: str = ""


@dataclass
class SnapshotReport:
    label: str = ""
    taken: list[Snapshot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def git(
    *args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True
) -> str:
    """Run git, returning stdout — and nothing at all on failure.

    Discarding stdout when git fails matters more than it looks: `rev-parse
    HEAD` in a repository with no commits exits 128 *and* echoes the literal
    string "HEAD" on stdout. Passing that through makes an unborn branch look
    like a valid commit.
    """

    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        if check:
            raise SnapshotError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return ""
    return result.stdout.strip()


class SnapshotError(RuntimeError):
    pass


def find_repos(root: Path, *, depth: int = 3, ignore: list[str] | None = None) -> list[Path]:
    """Every git repository at or below `root`, outermost first.

    A workspace can hold several repositories side by side, so this does not
    stop at the first one found. Directories without git are simply absent from
    the result — they are skipped, not an error.
    """

    skip = ALWAYS_SKIP | set(ignore or [])
    found: list[Path] = []

    if (top := toplevel(root)) is not None:
        found.append(top)

    def walk(directory: Path, remaining: int) -> None:
        if remaining < 0:
            return
        try:
            entries = sorted(p for p in directory.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            if entry.name in skip or entry.is_symlink():
                continue
            if (entry / ".git").exists():
                resolved = entry.resolve()
                if resolved not in found:
                    found.append(resolved)
            walk(entry, remaining - 1)

    walk(root, depth)
    return found


def ignore_locally(repo: Path, pattern: str) -> bool:
    """Hide a path from git for this checkout only.

    Written to `.git/info/exclude`, not `.gitignore`: dai's own bookkeeping is
    nobody else's business, so it must not turn up in the user's diff, in their
    commits, or in a colleague's checkout. Without this, `.dai/` shows up as
    untracked in every `git status` and gets swept into our own commits.
    """

    git_dir = git("rev-parse", "--absolute-git-dir", cwd=repo, check=False)
    if not git_dir:
        return False

    exclude = Path(git_dir) / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if any(line.strip() == pattern for line in existing.splitlines()):
            return False
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(f"{prefix}# added by dai\n{pattern}\n")
    except OSError:
        return False
    return True


def toplevel(path: Path) -> Path | None:
    """The root of the repository containing `path`, if there is one."""

    try:
        out = git("rev-parse", "--show-toplevel", cwd=path, check=False)
    except (OSError, SnapshotError):
        return None
    return Path(out).resolve() if out else None


class Snapshotter:
    """Commits the state of every repository in the workspace, once per round."""

    def __init__(
        self,
        cwd: Path,
        run_id: str,
        settings: SnapshotConfig | None = None,
    ) -> None:
        self.cwd = cwd
        self.run_id = run_id
        self.settings = settings or SnapshotConfig()
        self.repos: list[Path] = []
        #: Tip of this run's branch, per repository — the parent of the next commit.
        self._tips: dict[Path, str] = {}
        #: The branch name each repository actually accepted (see `_point`).
        self._branches: dict[Path, str] = {}
        if self.settings.enabled:
            self.repos = find_repos(
                cwd, depth=self.settings.scan_depth, ignore=self.settings.ignore
            )

    @property
    def active(self) -> bool:
        return bool(self.repos)

    @property
    def branch(self) -> str:
        """The branch name this run asks for; a repository may refuse it."""

        return f"{self.settings.branch_prefix}{self.run_id}"

    @property
    def branches(self) -> list[str]:
        """Every branch name actually written to, deduplicated."""

        return sorted(set(self._branches.values()))

    @property
    def report_branch(self) -> str:
        """One branch name to show the user.

        Repositories all but always agree; the list only diverges when one of
        them had to settle for a flat name, and a report is a pointer rather
        than a contract.
        """

        names = self.branches
        return names[0] if names else ""

    def branch_for(self, repo: Path) -> str:
        return self._branches.get(repo, self.branch)

    # --- the two moments a run commits ------------------------------------

    def capture_gate(self, round_no: int, note: str = "") -> SnapshotReport:
        """Commit at a round boundary.

        The gate for round 1 fires before any agent has moved, so what it finds
        is the baseline: your own uncommitted work, kept in a commit of its own
        so that everything after it is the agents' doing and nothing else. Every
        later gate fires once the previous round has been fully played out —
        solved, critiqued and judged — which is exactly what it records.
        """

        if round_no <= 1:
            return self.capture(
                "baseline", subject="baseline (your work in progress)", note=note
            )
        done = round_no - 1
        return self.capture(f"round-{done}", subject=f"round {done}", note=note)

    def capture_final(self, note: str = "") -> SnapshotReport:
        """Commit where the run stopped.

        Separate from the gates because the last round has no gate after it, and
        because a deadlock resolution adds one more solver turn past the last
        judged round — as does a run killed from the TUI.
        """

        return self.capture("final", note=note)

    def capture(self, label: str, *, subject: str = "", note: str = "") -> SnapshotReport:
        """Commit every repository, tolerating individual failures."""

        message = f"dai {self.run_id}: {subject or label}"
        if note:
            message = f"{message}\n\n{note}"

        report = SnapshotReport(label=label)
        for repo in self.repos:
            try:
                snapshot = self._capture_one(repo, label, message)
            except (SnapshotError, OSError) as exc:
                report.skipped.append(f"{repo.name}: {exc}")
                continue
            if snapshot is not None:
                report.taken.append(snapshot)
        return report

    # --- internals --------------------------------------------------------

    def _capture_one(self, repo: Path, label: str, message: str) -> Snapshot | None:
        # --verify keeps git quiet about an unborn branch; empty means no commits.
        head = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo, check=False)
        parent = self._tips.get(repo) or head

        with tempfile.TemporaryDirectory(prefix="dai-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}

            # Seed from the branch tip so deletions since it register, then
            # stage the whole tree. A repository with no commits yet, on its
            # first round, has nothing to seed from.
            if parent:
                git("read-tree", parent, cwd=repo, env=env)
            git("add", "-A", cwd=repo, env=env)
            tree = git("write-tree", cwd=repo, env=env)

        if not tree or self._is_unchanged(repo, tree, parent):
            # Nothing moved this round, so no commit: an argument that spends a
            # round talking should not leave an empty one in the history. The
            # branch still has to exist from the start, pointed at where we came
            # from, so it can be diffed and merged before the first real change.
            if parent:
                self._point(repo, parent, label)
            return None

        commit = git(
            "commit-tree",
            tree,
            *(["-p", parent] if parent else []),
            "-m",
            message,
            cwd=repo,
            env={
                # These are the tool's commits, not the user's.
                "GIT_AUTHOR_NAME": "dai",
                "GIT_AUTHOR_EMAIL": "dai@localhost",
                "GIT_COMMITTER_NAME": "dai",
                "GIT_COMMITTER_EMAIL": "dai@localhost",
            },
        )
        branch = self._point(repo, commit, label)
        self._tips[repo] = commit
        return Snapshot(repo=repo, branch=branch, commit=commit, label=label)

    def _is_unchanged(self, repo: Path, tree: str, parent: str) -> bool:
        if parent:
            return tree == git("rev-parse", f"{parent}^{{tree}}", cwd=repo, check=False)
        # No parent means a repository with no commits at all: the only tree
        # worth skipping is the empty one, and asking git for it keeps this
        # right in repositories that hash with something other than SHA-1.
        return tree == git("hash-object", "-t", "tree", os.devnull, cwd=repo, check=False)

    def _point(self, repo: Path, commit: str, label: str) -> str:
        """Move this run's branch to `commit`, returning the name it went by."""

        wanted = self._branches.get(repo) or self.branch
        reflog = f"dai {self.run_id} {label}"
        try:
            git("update-ref", "-m", reflog, f"refs/heads/{wanted}", commit, cwd=repo)
        except SnapshotError:
            if "/" not in wanted:
                raise
            # `refs/heads/dai/<run>` cannot exist beside a branch called `dai`:
            # git keeps refs as files, and a file cannot also be a directory.
            wanted = wanted.replace("/", "-").strip("-")
            git("update-ref", "-m", reflog, f"refs/heads/{wanted}", commit, cwd=repo)
        self._branches[repo] = wanted
        return wanted
