"""Recording a run and reading it back."""

from __future__ import annotations

import json
from pathlib import Path

from dai.__main__ import _apply_overrides, build_parser
from dai.budget import Spend
from dai.config import from_dict
from dai.models import (
    Action,
    CriticTurn,
    Issue,
    Outcome,
    Reply,
    Severity,
    SolverTurn,
    Verdict,
)
from dai.orchestrator import DebateResult, Round
from dai.snapshot import RepoResult
from dai.transcript import (
    Transcript,
    list_runs,
    new_run_id,
    read_events,
    render_report,
)


def sample_result(outcome=Outcome.CONSENSUS) -> DebateResult:
    issue = Issue(id="i1", severity=Severity.MAJOR, claim="Status column empty",
                  evidence="matrix.md:7", fix="fill from README")
    r1 = Round(
        number=1,
        solver=SolverTurn(summary="filled the table", files_changed=["matrix.md"]),
        critic=CriticTurn(verdict=Verdict.REQUEST_CHANGES, issues=[issue],
                          checked=["read matrix.md"], summary="incomplete"),
        notes=["critic approved without listing what it checked"],
    )
    r2 = Round(
        number=2,
        solver=SolverTurn(summary="fixed it",
                          replies=[Reply(id="i1", action=Action.FIXED, detail="done | now")]),
        critic=CriticTurn(verdict=Verdict.APPROVE, checked=["re-read matrix.md"],
                          conceded=[], summary="good"),
    )
    return DebateResult(
        outcome=outcome,
        reason="critic approved",
        rounds=[r1, r2],
        spend=Spend(turns=4, measured_usd=0.42, tokens=123456),
        open_issues=[] if outcome is Outcome.CONSENSUS else [issue],
    )


# --- run ids --------------------------------------------------------------


def test_run_ids_are_unique_and_sortable():
    ids = {new_run_id() for _ in range(50)}

    assert len(ids) == 50
    assert all(len(i.split("-")) == 3 for i in ids)


# --- writing --------------------------------------------------------------


def test_events_are_appended_as_jsonl(tmp_path):
    transcript = Transcript(tmp_path, "run1")
    transcript.start(task="do it", cwd=tmp_path, solver="claude", critic="codex")
    transcript.event("verdict", round=1, text="APPROVE")

    records = read_events(transcript.dir)

    assert [r["kind"] for r in records] == ["start", "verdict"]
    assert records[0]["task"] == "do it"
    assert all("ts" in r for r in records)


def test_finish_writes_a_readable_report(tmp_path):
    transcript = Transcript(tmp_path, "run1")
    path = transcript.finish(
        sample_result(), task="do it", cwd=tmp_path, solver="claude", critic="codex"
    )

    assert path is not None and path.name == "report.md"
    body = path.read_text()
    assert "# dai run run1" in body
    assert "Round 1" in body and "Round 2" in body


def test_a_read_only_workspace_does_not_break_the_run(tmp_path):
    """Recording is a convenience; failing to record must not stop the argument."""

    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        transcript = Transcript(blocked, "run1")
        transcript.event("start")  # must not raise
        assert not transcript.enabled
    finally:
        blocked.chmod(0o700)


def test_non_ascii_survives_the_round_trip(tmp_path):
    transcript = Transcript(tmp_path, "run1")
    transcript.start(task="añade la sección que falta", cwd=tmp_path, solver="claude", critic="codex")

    raw = (transcript.dir / "events.jsonl").read_text(encoding="utf-8")

    assert "añade la sección que falta" in raw
    assert json.loads(raw.splitlines()[0])["task"] == "añade la sección que falta"


# --- report content -------------------------------------------------------


def test_report_states_outcome_spend_and_participants(tmp_path):
    body = render_report(sample_result(), run_id="run1", task="do it",
                         cwd=tmp_path, solver="claude", critic="codex")

    assert "Agreed" in body
    assert "**Solver:** claude · **Critic:** codex" in body
    assert "123,456 tokens" in body
    assert "$0.42" in body


def test_report_flags_unmeasured_spend_rather_than_implying_zero():
    result = sample_result()
    result.spend = Spend(turns=2, measured_usd=0.1, tokens=99, unpriced={"codex"})

    body = render_report(result, run_id="r", task="t", cwd=Path("/tmp"),
                         solver="claude", critic="codex")

    assert "unmeasured spend by codex" in body


def test_report_records_the_argument_not_just_the_outcome():
    body = render_report(sample_result(), run_id="r", task="t", cwd=Path("/tmp"),
                         solver="claude", critic="codex")

    assert "Status column empty" in body      # what the critic claimed
    assert "matrix.md:7" in body              # the evidence it gave
    assert "| i1 | FIXED |" in body           # how the solver answered
    assert "Checked:" in body                 # what it verified


def test_report_escapes_pipes_so_the_table_survives():
    body = render_report(sample_result(), run_id="r", task="t", cwd=Path("/tmp"),
                         solver="claude", critic="codex")

    assert r"done \| now" in body


def test_unresolved_issues_are_called_out():
    body = render_report(sample_result(Outcome.DEADLOCK), run_id="r", task="t",
                         cwd=Path("/tmp"), solver="claude", critic="codex")

    assert "Left unresolved" in body
    assert "Deadlocked" in body


def report(repos, cwd=Path("/ws")) -> str:
    return render_report(sample_result(), run_id="run1", task="t", cwd=cwd,
                         solver="claude", critic="codex", repos=repos)


def test_report_explains_what_to_do_with_the_branch():
    """Named against the repository holding it: the branch alone is not an address.

    The directory a run starts in need not be a repository — it can hold
    several a level down — so a bare `git log HEAD..<branch>` typed there
    answers "not a git repository", which reads like nothing was committed.
    """

    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1",
                              base="a" * 40, commits=3)])

    assert "api" in body
    assert "git -C api log --oneline aaaaaaaaaaaa..dai/run1" in body
    assert "git -C api diff aaaaaaaaaaaa dai/run1" in body
    assert "git -C api reset --hard dai/run1" in body
    assert "git branch -D" in body
    assert "HEAD.." not in body


def test_commands_for_the_directory_you_are_in_carry_no_dash_c():
    body = report([RepoResult(repo=Path("/ws"), branch="dai/run1",
                              base="a" * 40, commits=1)], cwd=Path("/ws"))

    assert "git log --oneline aaaaaaaaaaaa..dai/run1" in body
    assert "git -C" not in body


def test_the_report_names_every_repository_that_was_committed_to():
    body = report([
        RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40, commits=2),
        RepoResult(repo=Path("/ws/web"), branch="dai/run1", base="b" * 40, commits=1),
        RepoResult(repo=Path("/ws/docs"), branch="dai/run1", base="c" * 40),
    ])

    assert "git -C api" in body
    assert "git -C web" in body
    assert "3 commits in 2 of 3 repositories" in body
    assert "docs" in body  # named as untouched, not silently dropped


def test_the_report_drops_the_commit_range_for_a_repository_with_no_history():
    body = report([RepoResult(repo=Path("/ws/fresh"), branch="dai/run1", commits=1)])

    assert "git -C fresh log --oneline dai/run1" in body
    assert ".." not in body.split("## The work")[1]


def test_a_run_that_changed_nothing_says_so_instead_of_going_quiet():
    """The reported bug: a finished run that looks like it did nothing.

    The branch is real and empty, which is a different thing from absent, and
    the report has to be the one that says which.
    """

    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40)])

    assert "Nothing on disk changed" in body
    assert "dai/run1" in body
    assert "reset --hard dai/run1" not in body


def test_the_report_says_your_branch_was_moved_when_the_work_was_merged():
    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                              commits=2, merged=True)])

    assert "already yours" in body
    assert "git -C api reset --hard dai/run1" not in body
    assert "git -C api reset --hard aaaaaaaaaaaa" in body  # the undo


def test_a_refused_merge_is_reported_with_its_reason():
    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                              commits=2, note="you have staged changes")])

    assert "Not merged: you have staged changes" in body
    assert "git -C api reset --hard dai/run1" in body  # still yours to take


def test_no_branch_means_no_git_advice():
    """A dry run commits nothing; naming a branch that does not exist is worse
    than saying nothing at all."""

    body = render_report(sample_result(), run_id="run1", task="t", cwd=Path("/tmp"),
                         solver="claude", critic="codex")

    assert "git reset" not in body
    assert "dai/run1" not in body


# --- listing --------------------------------------------------------------


def test_runs_are_listed_newest_first(tmp_path):
    for run_id, task in [("20260101-000000-aaaa", "first"), ("20260202-000000-bbbb", "second")]:
        t = Transcript(tmp_path, run_id)
        t.start(task=task, cwd=tmp_path, solver="claude", critic="codex")
        t.finish(sample_result(), task=task, cwd=tmp_path, solver="claude", critic="codex")

    runs = list_runs(tmp_path)

    assert [r.task for r in runs] == ["second", "first"]
    assert all(r.outcome == "consensus" for r in runs)


def test_listing_an_empty_workspace_is_not_an_error(tmp_path):
    assert list_runs(tmp_path) == []


def test_an_unfinished_run_still_lists(tmp_path):
    t = Transcript(tmp_path, "run1")
    t.start(task="interrupted", cwd=tmp_path, solver="claude", critic="codex")

    runs = list_runs(tmp_path)

    assert runs[0].task == "interrupted"
    assert runs[0].outcome == ""


# --- snapshot switches ----------------------------------------------------


def test_dry_run_disables_snapshots():
    """Nothing is written, so there is nothing worth recording."""

    args = build_parser().parse_args(["t", "--dry-run"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.enabled is False


def test_no_snapshot_flag_disables_snapshots():
    args = build_parser().parse_args(["t", "--no-snapshot"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.enabled is False


def test_snapshots_are_on_by_default():
    args = build_parser().parse_args(["t"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.enabled is True


def test_merging_is_off_unless_you_ask_for_it():
    """It moves the branch the user is standing on. Nobody gets that by accident."""

    args = build_parser().parse_args(["t"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge_on_consensus is False


def test_the_merge_flag_arms_it():
    args = build_parser().parse_args(["t", "--merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge_on_consensus is True


def test_a_dry_run_cannot_merge_anything():
    """Nothing was written, so there is no work to move a branch onto."""

    args = build_parser().parse_args(["t", "--dry-run", "--merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge_on_consensus is False


def test_the_branches_are_recorded_in_the_event_log(tmp_path):
    """Where the commits went has to survive in the record, not just on screen."""

    t = Transcript(tmp_path, "run1")
    t.branches([RepoResult(repo=tmp_path / "api", branch="dai/run1",
                           base="a" * 40, commits=2)])

    recorded = [e for e in read_events(t.dir) if e["kind"] == "branches"]

    assert recorded[0]["repos"][0]["branch"] == "dai/run1"
    assert recorded[0]["repos"][0]["commits"] == 2
    assert recorded[0]["repos"][0]["repo"].endswith("api")
