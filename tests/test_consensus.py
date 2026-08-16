"""The referee: telling real agreement from a rubber stamp, and debate from a loop."""

from __future__ import annotations

from dai.consensus import Referee
from dai.models import Action, CriticTurn, Issue, Reply, Severity, SolverTurn, Verdict


def issue(id="i1", claim="status column empty", severity=Severity.MAJOR, evidence="m.md:7"):
    return Issue(id=id, severity=severity, claim=claim, evidence=evidence)


def approve(checked=("read m.md",)):
    return CriticTurn(verdict=Verdict.APPROVE, checked=list(checked))


def changes(*issues, conceded=(), checked=("read m.md",)):
    return CriticTurn(
        verdict=Verdict.REQUEST_CHANGES,
        issues=list(issues),
        conceded=list(conceded),
        checked=list(checked),
    )


def rejected(*ids):
    return SolverTurn(replies=[Reply(id=i, action=Action.REJECTED, detail="you misread") for i in ids])


def fixed(*ids):
    return SolverTurn(replies=[Reply(id=i, action=Action.FIXED, detail="done") for i in ids])


# --- agreement ------------------------------------------------------------


def test_a_verified_approval_settles_the_argument():
    assessment = Referee().judge(approve())

    assert assessment.settled
    assert not assessment.rubber_stamp


def test_approval_without_evidence_of_checking_is_refused():
    """An approval nobody can audit is worthless; the loop must continue."""

    assessment = Referee().judge(approve(checked=()))

    assert not assessment.settled
    assert assessment.rubber_stamp
    assert "without saying what it checked" in assessment.reason


def test_an_approval_naming_no_changed_file_fails_the_audit():
    """`checked: ["reviewed the changes"]` used to pass everything there was."""

    reason = Referee().audit(
        approve(checked=("reviewed the changes",)),
        SolverTurn(files_changed=["src/parse.py"]),
    )

    assert reason is not None
    assert "naming any file the solver changed" in reason


def test_that_audit_can_cost_a_turn_but_never_the_run():
    """It reads prose and can be wrong, so only the orchestrator may act on it."""

    critic, solver = approve(checked=("reviewed the changes",)), SolverTurn(
        files_changed=["src/parse.py"]
    )

    assert Referee().judge(critic, solver=solver).settled


def test_naming_the_changed_file_passes_the_audit():
    critic = approve(checked=("/repo/src/parse.py:1-9 — matches the task",))

    assert Referee().audit(critic, SolverTurn(files_changed=["src/parse.py"])) is None


def test_the_audit_is_off_when_the_solver_reported_no_files():
    """The raw-text fallback for an unparseable solver report has no file list."""

    critic = approve(checked=("read what I could find",))

    assert Referee().audit(critic, SolverTurn(summary="prose")) is None


def test_an_empty_checked_fails_the_audit_with_or_without_the_solver():
    assert Referee().audit(approve(checked=())) is not None
    assert Referee().audit(approve(checked=()), SolverTurn(files_changed=["a.md"]))


def test_request_changes_is_never_audited_as_an_approval():
    assert Referee().audit(changes(issue()), SolverTurn(files_changed=["a.md"])) is None


def test_request_changes_with_everything_conceded_settles():
    assessment = Referee().judge(changes(issue(), conceded=["i1"]))

    assert assessment.settled


def test_only_minor_points_left_settles_by_default():
    assessment = Referee().judge(changes(issue(severity=Severity.MINOR)))

    assert assessment.settled
    assert "minor" in assessment.reason


def test_minor_points_can_be_made_blocking():
    assessment = Referee(stop_on_minor_only=False).judge(
        changes(issue(severity=Severity.MINOR))
    )

    assert not assessment.settled


def test_a_blocker_keeps_the_argument_open():
    assessment = Referee().judge(changes(issue(severity=Severity.BLOCKER)))

    assert not assessment.settled


# --- stalling -------------------------------------------------------------


def test_repeating_the_same_complaints_eventually_deadlocks():
    referee = Referee(no_progress_rounds=2)
    same = lambda: changes(issue())  # noqa: E731

    assert not referee.judge(same()).stale  # round 1: nothing to compare to
    assert not referee.judge(same()).stale  # round 2: stalled once
    assert referee.judge(same()).stale  # round 3: stalled twice → deadlock


def test_progress_resets_the_stall_counter():
    referee = Referee(no_progress_rounds=2)

    referee.judge(changes(issue()))
    referee.judge(changes(issue()))
    moved = referee.judge(changes(issue(claim="something else entirely")))
    assert not moved.stale

    # The counter restarts, so one repeat is not enough to deadlock again.
    assert not referee.judge(changes(issue(claim="something else entirely"))).stale


def test_deadlock_requires_disagreement():
    """Settled rounds are never reported as deadlocked, however repetitive."""

    referee = Referee(no_progress_rounds=1)
    referee.judge(changes(issue()))
    assessment = referee.judge(approve())

    assert assessment.settled
    assert not assessment.deadlocked


# --- refusing to engage ---------------------------------------------------


def test_reraising_a_rebutted_issue_unchanged_is_flagged():
    """The critic must answer the rebuttal, not restate the complaint."""

    referee = Referee()
    first = changes(issue(evidence="m.md:7"))
    referee.judge(first)

    again = referee.judge(
        changes(issue(id="i9", evidence="m.md:7")),
        previous=first,
        solver=rejected("i1"),
    )

    assert [i.id for i in again.repeats] == ["i9"]


def test_reraising_with_new_evidence_is_legitimate_argument():
    referee = Referee()
    first = changes(issue(evidence="m.md:7"))
    referee.judge(first)

    again = referee.judge(
        changes(issue(id="i9", evidence="ran `pytest`, it fails at test_x")),
        previous=first,
        solver=rejected("i1"),
    )

    assert again.repeats == []


def test_repeating_an_issue_the_solver_claims_fixed_is_not_a_repeat():
    """Re-checking a claimed fix is exactly the critic's job."""

    referee = Referee()
    first = changes(issue())
    referee.judge(first)

    again = referee.judge(changes(issue(id="i9")), previous=first, solver=fixed("i1"))

    assert again.repeats == []
