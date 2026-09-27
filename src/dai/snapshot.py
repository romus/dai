"""The agents work on a branch of their own, and you end up standing on it.

A repository that changes is moved onto `dai/<run-id>`, rooted at the branch you
would merge back into — `main`/`master` by default, or wherever you are
(`branch_from`). Each round lands there as an ordinary commit, and at consensus
the base branch is fast-forwarded onto the result, so afterwards `git status` is
clean and `git log` reads as the work having simply been done.

Repositories nothing changed in are not touched at all: no branch, no commit,
no switch.

Neither the switch nor the commits go through `git checkout` or `git commit`,
because both rewrite files and run your hooks — a formatter firing mid-round
would edit the tree under the agents' feet. Instead:

    update-ref refs/heads/dai/<run>  <start>   # create the branch
    symbolic-ref HEAD refs/heads/dai/<run>     # stand on it
    read-tree <start>                          # index matches the new HEAD

`read-tree` without `-u` writes only the index, so not one file on disk is
rewritten and no hook fires. A round commit is the same three steps with a
`commit-tree` in front of them.

What was uncommitted before the run began is preserved as a `baseline` commit of
its own, so everything after it is the agents' doing. The one thing that cannot
be preserved is a staged-then-edited blob — it lives only in the index, which
`read-tree` overwrites — so a repository with anything staged is left alone and
said so.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from dai.config import Merge, SnapshotConfig

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
    """What a run left in one repository, and where that leaves you standing.

    `base` is the commit the run branch was rooted at — the undo for the whole
    thing, and the left-hand side of every range the report prints. It is kept
    rather than derived because `HEAD..` stops being true the moment anything
    moves, and a report whose commands silently print nothing is worse than no
    report at all.

    One row, not two: the report, the terminal and the TUI all want the same
    line, and a second list keyed by path would have them joining it themselves.
    """

    repo: Path
    branch: str
    base: str = ""
    #: The branch the run was rooted on, and merges back into.
    base_branch: str = ""
    commits: int = 0
    #: Whether this repository was actually moved onto the run's branch.
    switched: bool = False
    merged: bool = False
    #: Why the merge did not happen, when one was attempted and refused.
    note: str = ""

    @property
    def touched(self) -> bool:
        return self.commits > 0

    @property
    def standing_on(self) -> str:
        """The branch you are left on in this repository."""

        return self.base_branch if self.merged else self.branch


@dataclass(frozen=True)
class MergeCandidate:
    """One repository's answer to "what would merging this write, and can it?"

    Everything a prompt prints as it stands: no git, no path arithmetic, no
    counting. That is the point of it — the question is asked before anything
    is written, so whoever is drawing it must not be able to write either.

    `repo` is the only field that travels back out, and it is the key the merge
    is then asked for by.
    """

    repo: Path
    #: How a row names it: "." for the workspace itself, else relative to it.
    label: str
    #: The branch this repository actually took — possibly the flat fallback.
    branch: str
    #: What it would merge into.
    base_branch: str
    added: int = 0
    removed: int = 0
    #: The changed file names. The renderers show the head and count the rest.
    files: tuple[str, ...] = ()
    #: Why this one cannot go. Empty means it can.
    refusal: str = ""
    #: Lines added and removed since the person last objected — the part of
    #: `added`/`removed` their extra round wrote. None when nobody objected.
    since: tuple[int, int] | None = None

    @property
    def mergeable(self) -> bool:
        return not self.refusal


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


def git_ok(*args: str, cwd: Path) -> bool:
    """Whether git succeeded, for the commands that answer with an exit code.

    `merge-base --is-ancestor` prints nothing either way, so `git()` above —
    which reads stdout — cannot tell yes from no. Anything asked as a question
    rather than for an answer has to come through here.
    """

    return (
        subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True
        ).returncode
        == 0
    )


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


def merge_promise(merge: Merge) -> str:
    """What the pre-run banner promises will happen to the work, if they agree.

    Said before the run rather than discovered after it, which is why "ask"
    cannot borrow "merged back": a run that is going to stop and ask has not
    promised anything yet, and saying otherwise is the promise being broken.
    """

    return {
        Merge.ALWAYS: " · merged back if they agree",
        Merge.ASK: " · you choose what to merge if they agree",
    }.get(merge, "")


def describe(repos: Sequence[RepoResult], cwd: Path) -> list[str]:
    """Where a run left its work, one repository per line, for a terminal.

    The long form lives in the report; this is what the run says on its way out,
    and it exists because a branch name on its own does not tell you which
    directory to stand in to see it.
    """

    if not repos:
        return ["no repository changed — nothing was committed"]

    lines = []
    for entry in repos:
        plural = "" if entry.commits == 1 else "s"
        where = f"{entry.commits} commit{plural} in {entry.repo.name}"
        if entry.merged:
            lines.append(f"{where}, merged into {entry.base_branch} — you are on it")
        else:
            lines.append(f"{where} on {entry.branch} — you are on it")
            if entry.note:
                lines.append(f"  not merged into {entry.base_branch}: {entry.note}")
        span = f"{entry.base[:12]}..{entry.standing_on}" if entry.base else entry.standing_on
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
        #: Everything below is recorded once, before any agent moves.
        self._seen: set[Path] = set()
        #: HEAD then — "" in a repository with no commits yet.
        self._heads: dict[Path, str] = {}
        #: The branch you were on then; "" for a detached HEAD.
        self._origin: dict[Path, str] = {}
        #: The working tree then, as a tree object: your work in progress.
        self._baseline: dict[Path, str] = {}
        #: Repositories left alone because the index held something we cannot keep.
        self._staged: set[Path] = set()
        #: Where the run branch was rooted: the base branch, and its tip then.
        self._base_branch: dict[Path, str] = {}
        self._base_tip: dict[Path, str] = {}
        #: Repositories actually moved onto the run's branch.
        self._switched: set[Path] = set()
        #: How many commits this run actually landed, per repository.
        self._counts: dict[Path, int] = {}
        #: What `merge` did, per repository: (moved, why it did not).
        self._merged: dict[Path, tuple[bool, str]] = {}
        #: Where each repository stood when the person last objected; None
        #: until somebody has. See `mark`.
        self._marks: dict[Path, str] | None = None
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

        Only repositories that actually changed are listed, because those are
        the only ones this run touched at all. One whose git refused us, or one
        left alone because something was staged, is reported as skipped instead.
        """

        rows = []
        for repo in self.repos:
            if repo not in self._switched:
                continue
            merged, note = self._merged.get(repo, (False, ""))
            rows.append(
                RepoResult(
                    repo=repo,
                    branch=self._branches[repo],
                    base=self._base_tip.get(repo, ""),
                    base_branch=self._base_branch.get(repo, ""),
                    commits=self._counts.get(repo, 0),
                    switched=True,
                    merged=merged,
                    note=note,
                )
            )
        return rows

    # --- the two moments a run commits ------------------------------------

    def capture_gate(self, round_no: int, note: str = "") -> SnapshotReport:
        """Commit at a round boundary.

        The gate for round 1 fires before any agent has moved, so it commits
        nothing: it only writes down where every repository stood, which is what
        makes the difference between your work in progress and the agents' work
        knowable later. Every later gate fires once the previous round has been
        fully played out — solved, critiqued and judged — which is exactly what
        it records.
        """

        if round_no <= 1:
            return self.observe()
        done = round_no - 1
        return self.capture(f"round-{done}", subject=f"round {done}", note=note)

    def observe(self) -> SnapshotReport:
        """Write down where every repository stands, and change nothing.

        By the time an agent has edited a file, your work in progress and its
        work are one indistinguishable tree. The only moment they can be told
        apart is this one, before either has happened.
        """

        report = SnapshotReport(label="baseline")
        for repo in self.repos:
            try:
                self._observe(repo, at_gate=True)
            except (SnapshotError, OSError) as exc:
                report.skipped.append(f"{repo.name}: {exc}")
                continue
            if repo in self._staged:
                report.skipped.append(
                    f"{repo.name}: you have staged changes, which moving onto a "
                    f"branch would discard — leaving this repo alone"
                )
        return report

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

    def mark(self) -> None:
        """Remember where every repository stands, before an extra round.

        So the merge prompt that follows can say which part of each diff the
        person's own objection bought. Read-only, like `preview`: it records
        tips we already hold and asks git nothing.
        """

        self._marks = dict(self._tips)

    def preview(self) -> list[MergeCandidate]:
        """What each repository is offering to merge, and whether it can.

        Read-only, and that is the whole contract: it is what makes "nothing is
        written until you choose" a fact about the code rather than a promise
        in a footer. Whoever asks the question renders these rows and hands
        back the paths — they never touch git themselves.
        """

        rows = []
        for entry in self.summary():
            since = None
            try:
                refusal = self._refusal(entry)
                added, removed, files = self._diffstat(entry)
                if self._marks is not None:
                    since = self._since(entry)
            except (SnapshotError, OSError) as exc:
                refusal, added, removed, files = str(exc), 0, 0, ()
            rows.append(
                MergeCandidate(
                    repo=entry.repo,
                    label=self._label(entry.repo),
                    branch=entry.branch,
                    base_branch=entry.base_branch,
                    added=added,
                    removed=removed,
                    files=files,
                    refusal=refusal,
                    since=since,
                )
            )
        return rows

    def merge(
        self, *, only: Collection[Path] | None = None, kept: str = ""
    ) -> list[RepoResult]:
        """Fast-forward the base branch onto the run's work, and stand on it.

        The target is not a separate setting: it is the branch the run was
        rooted on, so a fast-forward is possible by construction — unless that
        branch moved underneath us, which is the one case worth refusing.

        Every refusal comes back as a reason; nothing here raises, because
        losing the merge is survivable and losing the run's record over it is
        not. A refused repository simply stays on the run's branch, which is
        exactly where the work is.

        `only` narrows it to the repositories somebody picked; `kept` is the
        note the rest are recorded with. A repository left out is still asked
        for its refusal first, because one that was passed over because it
        *could not* go has to say so — "you kept the branch" over the top of a
        real reason would be the tool putting words in the user's mouth.
        """

        for entry in self.summary():
            try:
                if only is not None and entry.repo not in only:
                    reason = self._refusal(entry) or kept
                else:
                    reason = self._merge_one(entry)
            except (SnapshotError, OSError) as exc:
                reason = str(exc)
            self._merged[entry.repo] = (not reason, reason)
        return [entry for entry in self.summary() if entry.repo in self._merged]

    def _merge_one(self, entry: RepoResult) -> str:
        """Move the base branch onto the run's tip; the reason it did not."""

        if reason := self._refusal(entry):
            return reason

        repo, target = entry.repo, entry.base_branch
        git(
            "update-ref", "-m", f"dai {self.run_id} merge",
            f"refs/heads/{target}", self._tips[repo], cwd=repo,
        )
        # The index and working tree already match the tip, and the target now
        # points at it — so standing on it rewrites nothing.
        git("symbolic-ref", "HEAD", f"refs/heads/{target}", cwd=repo)
        return ""

    def _refusal(self, entry: RepoResult) -> str:
        """Why this repository cannot be fast-forwarded; empty when it can.

        Separate from the writes it guards so that it can be asked on its own,
        before anything has happened — which is what lets a prompt show a row
        it already knows is not going anywhere, and say why.
        """

        repo = entry.repo
        target = entry.base_branch
        if not self._tips.get(repo, ""):
            return "nothing was committed"
        if not target:
            return "there is no branch to merge into"

        here = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo, check=False)
        if here != entry.branch:
            return "you moved off the run's branch"

        now = git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{target}", cwd=repo,
            check=False,
        )
        if now != entry.base:
            # Fast-forwarding would drop whatever landed there meanwhile, and
            # rebasing on the user's behalf is not ours to decide.
            return self._moved_on(repo, target, entry.base, now)
        return ""

    def _moved_on(self, repo: Path, target: str, base: str, now: str) -> str:
        """That the branch moved, said in terms of what actually landed on it.

        "main moved while the agents were working" is true and tells you
        nothing; the file that moved is the thing you need in order to guess
        whether this is a conflict or a rename you can wave through.
        """

        landed = [
            name
            for name in git(
                "diff", "--name-only", base, now, cwd=repo, check=False
            ).splitlines()
            if name
        ]
        if not landed:
            return f"{target} moved while the agents were working"
        if len(landed) == 1:
            return f"{landed[0]} was edited on {target} too"
        return f"{landed[0]} and {len(landed) - 1} others were edited on {target} too"

    def _diffstat(self, entry: RepoResult) -> tuple[int, int, tuple[str, ...]]:
        """What merging this repository would write: lines either way, and what.

        The range starts at `base`, not at the run's first round commit, so the
        `baseline` commit carrying your own work in progress is counted too —
        because that is exactly what the fast-forward carries onto your branch.
        Showing less than the merge writes is the one lie not worth telling on
        a screen whose whole job is to say what is about to happen.
        """

        # A repository with no history at all has nothing to diff from; the
        # empty tree is what `_tree_of` already stands in with elsewhere.
        left = entry.base or self._tree_of(entry.repo, "")
        if not left:
            return 0, 0, ()
        return self._numstat(entry.repo, left, entry.branch)

    def _since(self, entry: RepoResult) -> tuple[int, int]:
        """Lines either way since the last objection, in this repository.

        A repository the extra round touched for the first time has its mark
        set where its branch was rooted (see `_capture_one`), so it counts
        from there — not from before your own work in progress.
        """

        mark = (self._marks or {}).get(entry.repo, "")
        # No mark means no history to root on when the extra round began: a
        # repository born during it, whose every line that round wrote.
        added, removed, _ = (
            self._numstat(entry.repo, mark, entry.branch)
            if mark
            else self._diffstat(entry)
        )
        return added, removed

    def _numstat(
        self, repo: Path, left: str, right: str
    ) -> tuple[int, int, tuple[str, ...]]:
        added = removed = 0
        files: list[str] = []
        listed = git("diff", "--numstat", left, right, cwd=repo, check=False)
        for line in listed.splitlines():
            plus, _, rest = line.partition("\t")
            minus, _, name = rest.partition("\t")
            if not name:
                continue
            # A binary file reports "-" in both columns. It still changed, so
            # it is still named; it simply has no lines to count.
            added += int(plus) if plus.isdigit() else 0
            removed += int(minus) if minus.isdigit() else 0
            files.append(name)
        return added, removed, tuple(files)

    def _label(self, repo: Path) -> str:
        """How a prompt names this repository: "." for the one you started in."""

        if repo == self.cwd:
            return "."
        try:
            return str(repo.relative_to(self.cwd))
        except ValueError:
            return str(repo)

    # --- internals --------------------------------------------------------

    def _observe(self, repo: Path, *, at_gate: bool = False) -> None:
        """Remember where a repository stood, once, before anything moves.

        `at_gate` is the round-1 gate, the only moment your work in progress and
        the agents' work are still separable. Arriving here any later — a caller
        that committed without gating first — means they no longer are, and the
        safe reading is that everything present is the run's: attributing your
        work to the agents is a mislabelled commit, while the other way round
        would drop their work on the floor.
        """

        if repo in self._seen:
            return
        self._seen.add(repo)
        # --verify keeps git quiet about an unborn branch; empty means no commits.
        head = git("rev-parse", "--verify", "--quiet", "HEAD", cwd=repo, check=False)
        self._heads[repo] = head
        self._origin[repo] = git(
            "symbolic-ref", "--quiet", "--short", "HEAD", cwd=repo, check=False
        )
        # `--name-only` rather than `--quiet`: the latter implies --exit-code,
        # so `git(check=False)` would answer "" for both outcomes. With no HEAD
        # to diff against, anything in the index is staged by definition.
        staged = (
            git("diff-index", "--cached", "--name-only", "HEAD", cwd=repo, check=False)
            if head
            else git("ls-files", "--cached", cwd=repo, check=False)
        )
        if staged:
            self._staged.add(repo)
        self._baseline[repo] = (
            self._stage_tree(repo, head) if at_gate else self._tree_of(repo, head)
        )

    def _capture_one(self, repo: Path, label: str, message: str) -> Snapshot | None:
        self._observe(repo)
        if repo in self._staged:
            # Said once, at the gate. Repeating it every round would drown the
            # rounds that did work.
            return None

        switched = repo in self._switched
        parent = self._tips.get(repo, "") if switched else self._heads.get(repo, "")
        tree = self._stage_tree(repo, parent)
        if not tree:
            return None

        # "Changed" means changed since the run began, not since your last
        # commit: before we switch, your own work in progress is already in the
        # baseline and must not read as a round's doing.
        was = self._baseline.get(repo, "") if not switched else self._tree_of(repo, parent)
        if tree == was:
            # A round spent talking should not leave an empty commit — and in a
            # repository that never changes, no branch either.
            return None

        if not switched:
            parent = self._begin(repo)
            if self._marks is not None:
                # First touched after an objection: all of it is that round's.
                self._marks.setdefault(repo, parent)

        commit = self._commit(repo, tree, parent, message)
        branch = self._point(repo, commit, label)
        if not switched:
            git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=repo)
            self._switched.add(repo)
        # Only the index, never `-u`: not one file on disk is rewritten, so no
        # hook of the user's can fire and nothing moves under the agents.
        git("read-tree", commit, cwd=repo)
        self._tips[repo] = commit
        self._counts[repo] = self._counts.get(repo, 0) + 1
        return Snapshot(repo=repo, branch=branch, commit=commit, label=label)

    def _begin(self, repo: Path) -> str:
        """Settle where this repository's run branch is rooted, and on what.

        Returns the parent for its first round commit: the base branch's tip,
        or a `baseline` commit holding whatever you had uncommitted, so that
        everything after it is the agents' doing and nothing else.
        """

        branch, tip = self._base_for(repo)
        self._base_branch[repo] = branch
        self._base_tip[repo] = tip

        baseline = self._baseline.get(repo, "")
        if baseline and baseline != self._tree_of(repo, tip):
            parent = self._commit(
                repo, baseline, tip,
                f"dai {self.run_id}: baseline (your work in progress)",
            )
            self._counts[repo] = self._counts.get(repo, 0) + 1
            return parent
        return tip

    def _base_for(self, repo: Path) -> tuple[str, str]:
        """The branch this run is rooted on, and where it pointed at the time."""

        here = self._origin.get(repo, "")
        head = self._heads.get(repo, "")
        wanted = self.settings.branch_from
        if wanted == "current":
            return here, head

        for name in ([wanted] if wanted != "default" else self._default_names(repo)):
            tip = git(
                "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo,
                check=False,
            )
            if not tip:
                continue
            # Rooting the run on a branch you are *ahead* of would fold your own
            # commits into one `baseline` and carry them back on the merge. Stay
            # where you are instead; that is the honest base.
            if head and tip != head and not self._is_ancestor(repo, head, tip):
                break
            return name, tip
        return here, head

    def _default_names(self, repo: Path) -> list[str]:
        """What this repository calls its trunk, most authoritative first."""

        names = []
        remote = git(
            "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=repo,
            check=False,
        )
        if remote.startswith("origin/"):
            names.append(remote.removeprefix("origin/"))
        return names + [n for n in ("main", "master", "trunk") if n not in names]

    def _is_ancestor(self, repo: Path, older: str, newer: str) -> bool:
        return git_ok("merge-base", "--is-ancestor", older, newer, cwd=repo)

    def _commit(self, repo: Path, tree: str, parent: str, message: str) -> str:
        return git(
            "commit-tree", tree, *(["-p", parent] if parent else []), "-m", message,
            cwd=repo,
            env={
                # These are the tool's commits, not the user's.
                "GIT_AUTHOR_NAME": "dai",
                "GIT_AUTHOR_EMAIL": AUTHOR,
                "GIT_COMMITTER_NAME": "dai",
                "GIT_COMMITTER_EMAIL": AUTHOR,
            },
        )

    def _tree_of(self, repo: Path, commit: str) -> str:
        if not commit:
            return git("hash-object", "-t", "tree", os.devnull, cwd=repo, check=False)
        return git("rev-parse", f"{commit}^{{tree}}", cwd=repo, check=False)

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
