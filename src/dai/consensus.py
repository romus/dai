"""Deciding when the argument is actually over.

A two-agent loop fails in two opposite directions. It can end too early, when
the critic waves the work through without looking — agreement that carries no
information. Or it can never end, when the critic keeps restating one objection
the solver has already answered. Neither is visible from a single round, so the
referee keeps history and judges the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dai.models import Action, CriticTurn, Issue, Severity, SolverTurn, Verdict


@dataclass
class Assessment:
    """The referee's read on the round just played."""

    settled: bool
    reason: str
    stale: bool = False
    #: Issues re-raised after a rebuttal without answering it.
    repeats: list[Issue] = field(default_factory=list)
    #: Critic approved without evidence of having checked anything.
    rubber_stamp: bool = False

    @property
    def deadlocked(self) -> bool:
        return self.stale and not self.settled


class Referee:
    """Watches the argument across rounds and calls it."""

    def __init__(
        self,
        *,
        no_progress_rounds: int = 2,
        stop_on_minor_only: bool = True,
    ) -> None:
        self.no_progress_rounds = max(1, no_progress_rounds)
        self.stop_on_minor_only = stop_on_minor_only
        self._fingerprints: list[frozenset[str]] = []
        self._stalled_rounds = 0

    def judge(
        self,
        critic: CriticTurn,
        *,
        previous: CriticTurn | None = None,
        solver: SolverTurn | None = None,
    ) -> Assessment:
        """Assess one critique in the context of the rounds before it."""

        repeats = self._find_unanswered_repeats(critic, previous, solver)
        self._track_progress(critic)
        stale = self._stalled_rounds >= self.no_progress_rounds

        if critic.verdict is Verdict.APPROVE:
            if not critic.checked:
                # An approval nobody can audit is worth nothing; make it argue.
                return Assessment(
                    settled=False,
                    reason="critic approved without saying what it checked",
                    stale=stale,
                    repeats=repeats,
                    rubber_stamp=True,
                )
            return Assessment(settled=True, reason="critic approved", repeats=repeats)

        open_issues = critic.open_issues
        if not open_issues:
            return Assessment(
                settled=True,
                reason="critic requested changes but left nothing open",
                repeats=repeats,
            )

        if self.stop_on_minor_only and all(
            issue.severity is Severity.MINOR for issue in open_issues
        ):
            return Assessment(
                settled=True,
                reason=f"only {len(open_issues)} minor point(s) remain",
                repeats=repeats,
            )

        return Assessment(
            settled=False,
            reason=f"{len(open_issues)} issue(s) still open",
            stale=stale,
            repeats=repeats,
        )

    # --- internals --------------------------------------------------------

    def _track_progress(self, critic: CriticTurn) -> None:
        """A round counts as progress if the set of live complaints moved."""

        current = frozenset(issue.fingerprint for issue in critic.open_issues)
        if self._fingerprints and current and current == self._fingerprints[-1]:
            self._stalled_rounds += 1
        else:
            self._stalled_rounds = 0
        self._fingerprints.append(current)

    @staticmethod
    def _find_unanswered_repeats(
        critic: CriticTurn,
        previous: CriticTurn | None,
        solver: SolverTurn | None,
    ) -> list[Issue]:
        """Complaints re-raised verbatim after the solver rebutted them.

        The protocol says a rejected issue may return only with new evidence
        that answers the objection. Restating it unchanged is the critic
        refusing to engage, and it is the main way these loops fail to converge.
        """

        if previous is None or solver is None:
            return []

        by_fingerprint = {issue.fingerprint: issue for issue in previous.issues}
        repeats = []
        for issue in critic.open_issues:
            earlier = by_fingerprint.get(issue.fingerprint)
            if earlier is None:
                continue
            reply = solver.reply_to(earlier.id)
            if reply is None or reply.action is not Action.REJECTED:
                continue
            if _normalise(issue.evidence) == _normalise(earlier.evidence):
                repeats.append(issue)
        return repeats


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())
