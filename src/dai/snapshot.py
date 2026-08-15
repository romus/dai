"""Per-round commits on a branch of dai's own, leaving your branch alone.

The obvious implementation — `git add -A && git commit` — is unacceptable: it
moves HEAD, rewrites the branch you are on and stages your own work-in-progress.

Instead each round is built in a throwaway index (`GIT_INDEX_FILE`), turned into
a commit with `commit-tree`, and chained onto a branch of dai's own,
`dai/<run-id>`. Nothing observable changes: not HEAD, not your branch, not the
index, not the working tree. `git commit` is never run either, so your
pre-commit hooks cannot rewrite files under the agents' feet.

What you gain is an ordinary branch with an ordinary linear history — one commit
per round, rooted at the HEAD you started from, ready for `git log`, `git diff`
and `git push` like anything else.

`merge()` is the one exception, and the only thing here that moves you: asked
for explicitly (`merge_on_consensus`), it fast-forwards the branch you were on
onto that history. It still runs no `git commit` and no `git merge`, so no hook
of yours fires — but it does move your branch and reset your index, which is
why it is opt-in and why it refuses rather than guesses the moment the
repository is not exactly where the run left it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dai.config import SnapshotConfig

#: Directories never worth walking into when looking for repositories.
ALWAYS_SKIP = {".git", "__pycache__"}

#: The identity every commit dai makes is authored by, which is also how they
#: are counted again later.
AUTHOR = "dai@localhost"


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


@dataclass(frozen=True)
class RepoResult:
    """What a run left in one repository.

    `base` is the commit the branch was rooted at, kept because `HEAD..branch`
    stops being true the moment the user's own HEAD moves — and a report whose
    commands silently print nothing is worse than no report. It is also the undo
    for a merge.

    One row, not two: the report, the terminal and the TUI all want the same
    line, and a second list keyed by path would have them joining it themselves.
    """

    repo: Path
    branch: str
    base: str = ""
    commits: int = 0
    merged: bool = False
    #: Why the merge did not happen, when one was attempted and refused.
    note: str = ""

    @property
    def touched(self) -> bool:
        return self.commits > 0


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


def flat_name(branch: str) -> str:
    """The name `_point` settles for when the slashed one cannot exist.

    `refs/heads/dai/<run>` cannot sit beside a branch called `dai`: git keeps
    refs as files, and a file cannot also be a directory. Anything that goes
    looking for this run's branches afterwards has to know both shapes, so the
    derivation lives here rather than twice.
    """

    return branch.replace("/", "-").strip("-")


def branch_shapes(prefix: str) -> list[str]:
    """Every ref pattern a run's branches can turn up under."""

    patterns = [f"refs/heads/{prefix}*"]
    if "/" in prefix:
        # Through `flat_name` itself, with the run id standing in as a glob, so
        # the shape we look for cannot drift from the shape `_point` writes.
        # Deriving it from the bare prefix would not do: `flat_name("dai/")` is
        # `"dai"`, and `dai*` also matches a branch of the user's called `dai`.
        patterns.append(f"refs/heads/{flat_name(prefix + '*')}")
    return patterns


def addressed(repo: Path, cwd: Path) -> str:
    """How to address a repository in a command the reader can paste.

    The workspace is not always the repository — it can hold several, or be no
    repository at all with every one of them a level down. A bare `git log` in
    the directory the run started from then answers "not a git repository",
    which reads exactly like "nothing was committed".
    """

    if repo == cwd:
        return "git"
    try:
        where = repo.relative_to(cwd)
    except ValueError:
        where = repo
    # A workspace with a space in its name still has to be pasteable.
    return f"git -C {shlex.quote(str(where))}"


def describe(repos: Sequence[RepoResult], cwd: Path) -> list[str]:
    """Where a run left its work, one repository per line, for a terminal.

    The long form lives in the report; this is what the run says on its way out,
    and it exists because a branch name on its own does not tell you which
    directory to stand in to see it.
    """

    if not repos:
        return []

    touched = [entry for entry in repos if entry.touched]
    if not touched:
        return ["no repository changed — nothing was committed"]

    lines = []
    for entry in touched:
        note = ""
        if entry.merged:
            note = " · merged into your branch"
        elif entry.note:
            note = f" · not merged: {entry.note}"
        plural = "" if entry.commits == 1 else "s"
        lines.append(
            f"{entry.commits} commit{plural} in {entry.repo.name} on {entry.branch}{note}"
        )
        span = f"{entry.base[:12]}..{entry.branch}" if entry.base else entry.branch
        lines.append(f"  {addressed(entry.repo, cwd)} log --oneline {span}")
    return lines


@dataclass(frozen=True)
class BranchInfo:
    repo: Path
    branch: str
    commits: int = 0
    subject: str = ""
    when: str = ""


def list_branches(root: Path, settings: SnapshotConfig | None = None) -> list[BranchInfo]:
    """Every branch dai has left at or below `root`, newest first per repository.

    Asking git for `dai/*` in one directory is not enough and was the reason a
    finished run could look like it had done nothing: the workspace need not be
    a repository at all, the branches can be one level down in any number of
    them, and a repository that already had a branch called `dai` took the flat
    name instead.

    Commits are counted by author rather than against HEAD. `_capture_one`
    forces the identity, so the count stays right after you have moved on, or
    merged the work, and cannot be inflated by a branch of yours that happens
    to match the prefix.
    """

    settings = settings or SnapshotConfig()
    found: list[BranchInfo] = []
    for repo in find_repos(root, depth=settings.scan_depth, ignore=settings.ignore):
        listed = git(
            "for-each-ref",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(committerdate:relative)%09%(contents:subject)",
            *branch_shapes(settings.branch_prefix),
            cwd=repo,
            check=False,
        )
        for line in listed.splitlines():
            name, _, rest = line.partition("\t")
            when, _, subject = rest.partition("\t")
            counted = git(
                "rev-list", "--count", f"--author={AUTHOR}", name, cwd=repo, check=False
            )
            found.append(
                BranchInfo(
                    repo=repo,
                    branch=name,
                    commits=int(counted) if counted.isdigit() else 0,
                    subject=subject,
                    when=when,
                )
            )
    return found


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
        #: The HEAD each repository was sitting on when this run first touched it.
        self._bases: dict[Path, str] = {}
        #: And the branch it was on then — "" for a detached HEAD. Kept as well
        #: as the commit, because switching to another branch that happens to
        #: point at the same commit must not read as "you never moved".
        self._on_branch: dict[Path, str] = {}
        #: How many commits this run actually landed, per repository.
        self._counts: dict[Path, int] = {}
        #: What `merge` did, per repository: (moved, why it did not).
        self._merged: dict[Path, tuple[bool, str]] = {}
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

    def branch_for(self, repo: Path) -> str:
        return self._branches.get(repo, self.branch)

    def summary(self) -> list[RepoResult]:
        """What this run left, repository by repository, in discovery order.

        A single branch name is not enough to find the work: a workspace can
        hold repositories side by side — or be no repository at all, with
        everything one level down — and a branch name on its own does not say
        which of them to stand in.

        Only repositories this run actually pointed a branch in are listed. One
        whose git refused us never got that far and is reported as skipped
        instead.
        """

        rows = []
        for repo in self.repos:
            if repo not in self._branches:
                continue
            merged, note = self._merged.get(repo, (False, ""))
            rows.append(
                RepoResult(
                    repo=repo,
                    branch=self._branches[repo],
                    base=self._bases.get(repo, ""),
                    commits=self._counts.get(repo, 0),
                    merged=merged,
                    note=note,
                )
            )
        return rows

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

    # --- adopting the work ------------------------------------------------

    def merge(self) -> list[RepoResult]:
        """Fast-forward the branch you were on onto this run's work.

        A plain `git merge --ff-only` will not do it: the working tree still
        holds the agents' changes uncommitted, and git refuses to overwrite them
        even with byte-identical content of its own. `reset --hard` onto a tree
        the working tree already matches moves the branch and rewrites no file.

        "Already matches" is the whole safety argument, so it is checked rather
        than assumed, along with everything else that would make the reset
        destructive. Every refusal comes back as a reason; nothing here raises,
        because losing the merge is survivable and losing the run's record over
        it is not.
        """

        for entry in self.summary():
            if not entry.touched:
                continue  # a branch sitting on your own HEAD has nothing to give
            try:
                reason = self._merge_one(entry)
            except (SnapshotError, OSError) as exc:
                reason = str(exc)
            self._merged[entry.repo] = (not reason, reason)
        return [entry for entry in self.summary() if entry.repo in self._merged]

    def _merge_one(self, entry: RepoResult) -> str:
        """Move the current branch onto the run's tip; the reason it did not."""

        repo = entry.repo
        tip = self._tips.get(repo, "")
        if not tip:
            return "nothing was committed"

        # Fast-forwarding a detached HEAD would move nothing findable again.
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo, check=False)
        if not branch:
            return "you are not on a branch"
        if branch != self._on_branch.get(repo, ""):
            return "you switched branch while the agents were working"

        head = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo, check=False)
        if head != entry.base:
            return "your branch moved while the agents were working"

        # A reset throws the index away. Content staged but since edited exists
        # in no commit of ours — we only ever recorded the working tree — so it
        # would survive in no ref at all. Note `--name-only` rather than
        # `--quiet`: the latter implies --exit-code, and `git(check=False)`
        # answers "" for both outcomes. With no HEAD to diff against, anything
        # in the index is staged by definition.
        staged = (
            git("diff-index", "--cached", "--name-only", "HEAD", cwd=repo)
            if head
            else git("ls-files", "--cached", cwd=repo)
        )
        if staged:
            return "you have staged changes a reset would discard"

        # `reset --hard` also throws away whatever the working tree holds. Safe
        # only while that is exactly what we committed a moment ago.
        wanted = git("rev-parse", f"{tip}^{{tree}}", cwd=repo, check=False)
        if not wanted or self._stage_tree(repo, tip) != wanted:
            return "the working tree changed after the last commit"

        git("reset", "--hard", tip, cwd=repo)
        return ""

    # --- internals --------------------------------------------------------

    def _capture_one(self, repo: Path, label: str, message: str) -> Snapshot | None:
        # --verify keeps git quiet about an unborn branch; empty means no commits.
        head = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo, check=False)
        parent = self._tips.get(repo) or head
        # Where this repository stood before we touched it, remembered once:
        # asking again later would answer with wherever the user has moved to.
        # Membership, not truthiness — an unborn repo's base is legitimately "".
        if repo not in self._bases:
            self._bases[repo] = head
            self._on_branch[repo] = git(
                "symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo, check=False
            )

        tree = self._stage_tree(repo, parent)

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
        self._counts[repo] = self._counts.get(repo, 0) + 1
        return Snapshot(repo=repo, branch=branch, commit=commit, label=label)

    def _stage_tree(self, repo: Path, parent: str) -> str:
        """The working tree as a git tree object, leaving the real index alone.

        Seeded from `parent` so that deletions since it register, then staged
        whole. A repository with no commits yet has nothing to seed from.

        Both the commit path and the pre-merge safety check go through here:
        two implementations of "what does the working tree hash to" would
        eventually disagree, and the one place that matters is the check
        standing between the user and a `reset --hard`.
        """

        with tempfile.TemporaryDirectory(prefix="dai-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if parent:
                git("read-tree", parent, cwd=repo, env=env)
            git("add", "-A", cwd=repo, env=env)
            return git("write-tree", cwd=repo, env=env)

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
            wanted = flat_name(wanted)
            git("update-ref", "-m", reflog, f"refs/heads/{wanted}", commit, cwd=repo)
        self._branches[repo] = wanted
        return wanted
