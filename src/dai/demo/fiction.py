"""Everything the demo makes up, in one place.

The merge dialog is only ever reached through `DaiApp._settle_merge`, which
wants a snapshotter that is active, a `preview()` with rows in it, and a run
that agreed. So rather than fake the *screen*, the demo fakes the *snapshotter*
— and then the application really is the application: the banner, the round
gate, the ledger, the dialog and the choice you make in it all run their own
code, and only the answers coming back from git are invented.
"""

from __future__ import annotations

from pathlib import Path

from dai.config import Merge, SnapshotConfig
from dai.snapshot import MergeCandidate, RepoResult, SnapshotReport, Snapshotter

#: Not a timestamped run id: nothing on screen should look like a branch you
#: could go and check out afterwards.
BRANCH = "dai/demo"

#: The repositories the demo pretends to have found. Three, because a workspace
#: holding several is the case the merge dialog exists for.
REPOS = ("service", "packages/engine", "vendor/prompts")


def candidates(root: Path) -> list[MergeCandidate]:
    """What the run would offer to merge, if any of this were real.

    The third one cannot go. That row — the reason spelled out, the tick
    withheld — is the state a real run cannot be talked into producing on
    demand, so the one place it can be shown reliably is here.
    """

    return [
        MergeCandidate(
            repo=root / REPOS[0], label=REPOS[0], branch=BRANCH, base_branch="main",
            added=318, removed=41, files=("index.html", "primes.html"),
        ),
        MergeCandidate(
            repo=root / REPOS[1], label=REPOS[1], branch=BRANCH, base_branch="develop",
            added=96, removed=12,
            files=("solver.py", "arbiter.py") + tuple(f"round{n}.py" for n in range(9)),
        ),
        MergeCandidate(
            repo=root / REPOS[2], label=REPOS[2], branch=BRANCH, base_branch="main",
            added=4, files=("critic.md",),
            refusal="critic.md was edited on main too",
        ),
    ]


class PretendSnapshotter(Snapshotter):
    """Answers every question a run asks about git, and touches none of it.

    Subclassed rather than mocked so that what you are looking at is the real
    thing with invented answers, not a puppet show with the same silhouette.

    It is constructed *disabled* so the base class never walks the filesystem
    looking for repositories, and then says it is active anyway — which is the
    only sleight of hand in here, and the reason nothing below it ever runs.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root, "demo", SnapshotConfig(enabled=False, merge=Merge.ASK))
        self.repos = [root / name for name in REPOS]
        #: What you picked, kept only so the closing note can say so.
        self.chosen: tuple[Path, ...] = ()
        self.declined = False

    @property
    def active(self) -> bool:
        return True

    @property
    def branch(self) -> str:
        return BRANCH

    def observe(self) -> None:
        pass

    def capture_gate(self, number: int, label: str = "") -> SnapshotReport:
        return SnapshotReport(label=f"round {number}")

    def capture(self, label: str = "") -> SnapshotReport:
        return SnapshotReport(label=label)

    def capture_final(self, label: str = "") -> SnapshotReport:
        return SnapshotReport(label=label)

    def preview(self) -> list[MergeCandidate]:
        return candidates(self.cwd)

    def merge(self, *, only=None, kept: str = "") -> list[RepoResult]:
        picked = tuple(candidates(self.cwd)) if only is None else tuple(only)
        self.chosen = picked
        self.declined = only is not None and not picked
        return self.summary()

    def summary(self) -> list[RepoResult]:
        return [
            RepoResult(
                repo=row.repo,
                branch=row.branch,
                # Left empty so the ledger prints no commit range: an invented
                # sha would read as one you could go and look up.
                base="",
                base_branch=row.base_branch,
                commits=2,
                switched=True,
                merged=row.repo in self.chosen,
                note="" if row.mergeable else row.refusal,
            )
            for row in candidates(self.cwd)
        ]
