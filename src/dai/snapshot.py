"""Per-round git snapshots that leave the user's repository state alone.

The obvious implementation — commit before each round — is unacceptable: it
moves HEAD, rewrites the branch and stages the user's own work-in-progress.

Instead each snapshot is built in a throwaway index (`GIT_INDEX_FILE`), turned
into a commit with `commit-tree`, and parked on a ref under `refs/dai/`. Nothing
observable changes: not HEAD, not the branch, not the index, not the working
tree. What you gain is `git diff refs/dai/<run>/r1 refs/dai/<run>/r2` and the
ability to recover any round.
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
    ref: str
    commit: str


@dataclass
class SnapshotReport:
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
    untracked in every `git status` and gets swept into our own snapshots.
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
    """Records the state of every repository in the workspace, once per round."""

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
        if self.settings.enabled:
            self.repos = find_repos(
                cwd, depth=self.settings.scan_depth, ignore=self.settings.ignore
            )

    @property
    def active(self) -> bool:
        return bool(self.repos)

    def capture(self, label: str) -> SnapshotReport:
        """Snapshot every repository, tolerating individual failures."""

        report = SnapshotReport()
        for repo in self.repos:
            try:
                snapshot = self._capture_one(repo, label)
            except (SnapshotError, OSError) as exc:
                report.skipped.append(f"{repo.name}: {exc}")
                continue
            if snapshot is not None:
                report.taken.append(snapshot)
        return report

    def ref_for(self, label: str) -> str:
        return f"refs/dai/{self.run_id}/{label}"

    def _capture_one(self, repo: Path, label: str) -> Snapshot | None:
        # --verify keeps git quiet about an unborn branch; empty means no commits.
        head = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo, check=False)

        with tempfile.TemporaryDirectory(prefix="dai-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}

            # Seed from HEAD so deletions register, then stage the whole tree.
            # A repository with no commits yet has no HEAD to seed from.
            if head:
                git("read-tree", head, cwd=repo, env=env)
            git("add", "-A", cwd=repo, env=env)
            tree = git("write-tree", cwd=repo, env=env)

        if not tree:
            return None

        parents = ["-p", head] if head else []
        commit = git(
            "commit-tree",
            tree,
            *parents,
            "-m",
            f"dai {self.run_id} {label}",
            cwd=repo,
            env={
                # Snapshots are the tool's bookkeeping, not the user's commits.
                "GIT_AUTHOR_NAME": "dai",
                "GIT_AUTHOR_EMAIL": "dai@localhost",
                "GIT_COMMITTER_NAME": "dai",
                "GIT_COMMITTER_EMAIL": "dai@localhost",
            },
        )
        ref = self.ref_for(label)
        git("update-ref", ref, commit, cwd=repo)
        return Snapshot(repo=repo, ref=ref, commit=commit)

    def diff_command(self, first: str, second: str) -> str:
        return f"git diff {self.ref_for(first)} {self.ref_for(second)}"
