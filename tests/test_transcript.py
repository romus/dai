"""Recording a run and reading it back."""

from __future__ import annotations

import json
from pathlib import Path

from dai.__main__ import _apply_overrides, build_parser
from dai.budget import Spend
from dai.config import Merge, from_dict
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
    find_run,
    list_all_runs,
    list_runs,
    new_run_id,
    pid_alive,
    read_events,
    render_report,
    run_dir,
    run_started,
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


def test_a_read_only_workspace_is_still_recorded(tmp_path, dai_home):
    """The record lives under `~/.dai`, so the workspace is never written to."""

    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        transcript = Transcript(blocked, "run1")
        transcript.start(task="t", cwd=blocked, solver="claude", critic="codex")
        assert transcript.enabled
        assert (transcript.dir / "events.jsonl").is_file()
        assert transcript.dir.is_relative_to(dai_home)
        assert list(blocked.iterdir()) == []
    finally:
        blocked.chmod(0o700)


def test_an_unwritable_home_does_not_break_the_run(tmp_path, dai_home):
    """Recording is a convenience; failing to record must not stop the argument."""

    parent = dai_home.parent
    parent.chmod(0o500)
    try:
        transcript = Transcript(tmp_path, "run1")
        transcript.event("start")  # must not raise
        assert not transcript.enabled
    finally:
        parent.chmod(0o700)


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


def test_report_explains_where_the_work_is(tmp_path):
    """Named against the repository holding it: the branch alone is not an address.

    The directory a run starts in need not be a repository — it can hold
    several a level down — so a bare `git log HEAD..<branch>` typed there
    answers "not a git repository", which reads like nothing was committed.
    """

    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                              base_branch="main", commits=3, switched=True)])

    assert "api" in body
    assert "You are on `dai/run1`" in body
    assert "git -C api log --oneline aaaaaaaaaaaa..dai/run1" in body
    assert "git -C api diff aaaaaaaaaaaa dai/run1" in body
    assert "git -C api checkout main" in body   # how to get back
    assert "HEAD.." not in body


def test_commands_for_the_directory_you_are_in_carry_no_dash_c():
    body = report([RepoResult(repo=Path("/ws"), branch="dai/run1", base="a" * 40,
                              base_branch="main", commits=1, switched=True)],
                  cwd=Path("/ws"))

    assert "git log --oneline aaaaaaaaaaaa..dai/run1" in body
    assert "git -C" not in body


def test_the_report_names_every_repository_that_was_committed_to():
    body = report([
        RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                   base_branch="main", commits=2, switched=True),
        RepoResult(repo=Path("/ws/web"), branch="dai/run1", base="b" * 40,
                   base_branch="main", commits=1, switched=True),
    ])

    assert "git -C api" in body
    assert "git -C web" in body
    assert "3 commits in 2 repositories" in body


def test_the_report_drops_the_commit_range_for_a_repository_with_no_history():
    body = report([RepoResult(repo=Path("/ws/fresh"), branch="dai/run1",
                              base_branch="main", commits=1, switched=True)])

    assert "git -C fresh log --oneline dai/run1" in body
    assert ".." not in body.split("## The work")[1]


def test_a_run_that_touched_no_repository_says_nothing_about_branches():
    """Naming a branch that was never created is worse than silence."""

    body = report([])

    assert "## The work" not in body
    assert "dai/run1" not in body


def test_the_report_says_you_are_on_your_own_branch_once_merged():
    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                              base_branch="main", commits=2, switched=True, merged=True)])

    assert "Merged into `main`" in body
    assert "`git status` is clean" in body
    assert "git -C api log --oneline aaaaaaaaaaaa..main" in body
    assert "git -C api reset --hard aaaaaaaaaaaa" in body  # the undo
    assert "checkout" not in body  # you are already where you want to be


def test_a_refused_merge_is_reported_with_its_reason():
    body = report([RepoResult(repo=Path("/ws/api"), branch="dai/run1", base="a" * 40,
                              base_branch="main", commits=2, switched=True,
                              note="main moved while the agents were working")])

    assert "Not merged into `main`: main moved" in body
    assert "Nothing is lost" in body
    assert "You are on `dai/run1`" in body


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


def test_a_prompt_backed_out_of_is_not_a_run(tmp_path):
    """A screenshot pasted and then abandoned leaves the directory, not a run."""

    images = run_dir(tmp_path, "20260101-000000-aaaa") / "images"
    images.mkdir(parents=True)
    (images / "img1.png").write_bytes(b"x")

    assert list_runs(tmp_path) == []


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


def test_merging_back_defaults_to_asking_you():
    args = build_parser().parse_args(["t"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge is Merge.ASK


def test_the_no_merge_flag_leaves_you_on_the_runs_branch():
    args = build_parser().parse_args(["t", "--no-merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge is Merge.NEVER


def test_the_merge_flag_skips_the_question():
    args = build_parser().parse_args(["t", "--merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge is Merge.ALWAYS


def test_the_ask_merge_flag_beats_a_config_that_had_made_up_its_mind():
    args = build_parser().parse_args(["t", "--ask-merge"])
    cfg = _apply_overrides(from_dict({"snapshot": {"merge": True}}), args)

    assert cfg.snapshot.merge is Merge.ASK


def test_the_more_cautious_merge_flag_wins_if_you_give_two():
    args = build_parser().parse_args(["t", "--merge", "--ask-merge", "--no-merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge is Merge.NEVER


def test_the_branch_root_can_be_named_on_the_command_line():
    args = build_parser().parse_args(["t", "--branch-from", "develop"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.branch_from == "develop"


def test_a_dry_run_cannot_merge_anything():
    """Nothing was written, so there is no work to move a branch onto."""

    args = build_parser().parse_args(["t", "--dry-run", "--merge"])
    cfg = _apply_overrides(from_dict({}), args)

    assert cfg.snapshot.merge is Merge.NEVER


def test_the_branches_are_recorded_in_the_event_log(tmp_path):
    """Where the commits went has to survive in the record, not just on screen."""

    t = Transcript(tmp_path, "run1")
    t.branches([RepoResult(repo=tmp_path / "api", branch="dai/run1",
                           base="a" * 40, commits=2)])

    recorded = [e for e in read_events(t.dir) if e["kind"] == "branches"]

    assert recorded[0]["repos"][0]["branch"] == "dai/run1"
    assert recorded[0]["repos"][0]["commits"] == 2
    assert recorded[0]["repos"][0]["repo"].endswith("api")



# --- where runs live ------------------------------------------------------


def record(workdir: Path, run_id: str, task: str = "t", *, finish: bool = True) -> Transcript:
    t = Transcript(workdir, run_id)
    t.start(task=task, cwd=workdir, solver="claude", critic="codex")
    if finish:
        t.finish(sample_result(), task=task, cwd=workdir, solver="claude", critic="codex")
    return t


def test_a_run_is_kept_under_the_dai_home_and_never_in_the_workdir(tmp_path, dai_home):
    t = record(tmp_path, "run1")

    assert t.dir.is_relative_to(dai_home / "projects")
    assert (t.dir / "report.md").is_file()
    assert list(tmp_path.iterdir()) == []


def test_two_directories_do_not_see_each_other_s_runs(tmp_path):
    api, web = tmp_path / "api", tmp_path / "web"
    api.mkdir(), web.mkdir()
    record(api, "20260101-000000-aaaa", "api work")
    record(web, "20260102-000000-bbbb", "web work")

    assert [r.task for r in list_runs(api)] == ["api work"]
    assert [r.task for r in list_runs(web)] == ["web work"]
    assert [r.task for r in list_all_runs()] == ["web work", "api work"]


def test_a_run_remembers_where_it_worked_and_who_ran_it(tmp_path):
    import os

    record(tmp_path, "run1", finish=False)

    info = list_runs(tmp_path)[0]
    assert info.cwd == str(tmp_path)
    assert info.pid == os.getpid()
    assert not info.finished


def test_a_run_is_found_here_first_and_then_anywhere(tmp_path):
    here, there = tmp_path / "here", tmp_path / "there"
    here.mkdir(), there.mkdir()
    record(there, "20260101-000000-aaaa")

    assert find_run("20260101-000000-aaaa", here) == run_dir(there, "20260101-000000-aaaa")
    assert find_run("20260101-000000-zzzz", here) is None


def test_a_run_id_cannot_walk_out_of_the_runs_directory(tmp_path):
    record(tmp_path, "run1")

    for hostile in ("../run1", "..", ".", "a/b", "*", ""):
        assert find_run(hostile, tmp_path) is None


def test_a_run_s_age_comes_from_its_id_or_else_its_directory(tmp_path):
    from datetime import datetime

    assert run_started("20260101-093000-aaaa", tmp_path) == datetime(2026, 1, 1, 9, 30)
    stamp = datetime.fromtimestamp(tmp_path.stat().st_mtime)
    assert run_started("run1", tmp_path) == stamp


def test_only_a_real_pid_is_ever_signalled(monkeypatch):
    """pid 0 is our own process group and -1 is everyone: never signal those."""

    import os

    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append(pid))
    for pid in (0, -1, None, "123", True, 1.5):
        assert pid_alive(pid) is False
    assert sent == []

    assert pid_alive(4242) is True
    assert sent == [4242]


def test_a_dead_pid_is_dead_and_somebody_else_s_is_alive(monkeypatch):
    import os

    def gone(pid, sig):
        raise ProcessLookupError

    def foreign(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", gone)
    assert pid_alive(4242) is False
    monkeypatch.setattr(os, "kill", foreign)
    assert pid_alive(4242) is True
