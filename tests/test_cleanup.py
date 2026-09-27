"""`dai --clean`: what is chosen for deletion, what is kept, and why."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dai import cleanup, home
from dai.transcript import Transcript, run_dir

from test_transcript import sample_result

NOW = datetime(2026, 9, 27, 12, 0)
LONG_AGO = NOW - timedelta(days=90)


def nobody(pid) -> bool:
    return False


def record(workdir: Path, run_id: str, *, finish: bool = True, pid: int | None = 4242) -> Path:
    t = Transcript(workdir, run_id)
    t.event("start", task=f"task {run_id}", cwd=str(workdir), solver="claude",
            critic="codex", **({"pid": pid} if pid is not None else {}))
    if finish:
        t.finish(sample_result(), task="t", cwd=workdir, solver="claude", critic="codex")
    return t.dir


def age(directory: Path, when: datetime) -> None:
    """Backdate everything in a run, so the no-pid grace rule sees it as old."""

    stamp = when.timestamp()
    for top, dirs, files in os.walk(directory):
        for name in [*files, *dirs]:
            os.utime(os.path.join(top, name), (stamp, stamp))
    os.utime(directory, (stamp, stamp))


def ids(candidates) -> list[str]:
    return [c.run_id for c in candidates]


@pytest.fixture
def project(tmp_path) -> Path:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    return workdir


def test_finished_runs_are_chosen_newest_first(project):
    record(project, "20260101-000000-aaaa")
    record(project, "20260201-000000-bbbb")

    chosen = cleanup.plan(project, now=NOW, alive=nobody)

    assert ids(chosen.remove) == ["20260201-000000-bbbb", "20260101-000000-aaaa"]
    assert chosen.running == [] and chosen.newer == 0
    assert chosen.size > 0


def test_a_run_whose_process_is_alive_is_kept(project):
    record(project, "20260101-000000-aaaa", finish=False, pid=4242)

    chosen = cleanup.plan(project, now=NOW, alive=lambda pid: pid == 4242)

    assert chosen.remove == []
    assert ids(chosen.running) == ["20260101-000000-aaaa"]
    assert chosen.running[0].pid == 4242


def test_a_run_whose_process_died_is_fair_game(project):
    record(project, "20260101-000000-aaaa", finish=False, pid=4242)

    chosen = cleanup.plan(project, now=NOW, alive=nobody)

    assert ids(chosen.remove) == ["20260101-000000-aaaa"]


def test_a_finished_run_is_never_asked_about_its_process(project):
    record(project, "20260101-000000-aaaa", pid=4242)
    asked = []

    cleanup.plan(project, now=NOW, alive=lambda pid: asked.append(pid) or True)

    assert asked == []


def test_a_fresh_directory_with_no_pid_may_be_an_open_prompt(project):
    """A screenshot pasted at the task prompt comes before any event does."""

    images = run_dir(project, "20260927-115900-aaaa") / "images"
    images.mkdir(parents=True)
    (images / "img1.png").write_bytes(b"png")

    chosen = cleanup.plan(project, now=datetime.now(), alive=nobody)

    assert chosen.remove == []
    assert ids(chosen.running) == ["20260927-115900-aaaa"]


def test_an_old_directory_with_no_pid_is_abandoned(project):
    images = run_dir(project, "20260101-000000-aaaa") / "images"
    images.mkdir(parents=True)
    (images / "img1.png").write_bytes(b"png")
    age(images.parent, LONG_AGO)

    chosen = cleanup.plan(project, now=NOW, alive=nobody)

    assert ids(chosen.remove) == ["20260101-000000-aaaa"]


def test_older_than_keeps_the_recent_ones(project):
    record(project, "20260101-000000-aaaa")
    record(project, "20260920-000000-bbbb")

    chosen = cleanup.plan(project, older_than=30, now=NOW, alive=nobody)

    assert ids(chosen.remove) == ["20260101-000000-aaaa"]
    assert chosen.newer == 1


def test_a_run_id_with_no_timestamp_is_aged_by_its_directory(project):
    old = record(project, "run1")
    age(old, LONG_AGO)
    record(project, "run2")  # just written

    chosen = cleanup.plan(project, older_than=30, now=datetime.now(), alive=nobody)

    assert ids(chosen.remove) == ["run1"]
    assert chosen.newer == 1


def test_one_project_is_cleaned_and_the_others_left_alone(tmp_path):
    api, web = tmp_path / "api", tmp_path / "web"
    api.mkdir(), web.mkdir()
    record(api, "20260101-000000-aaaa")
    record(web, "20260102-000000-bbbb")

    assert ids(cleanup.plan(api, now=NOW, alive=nobody).remove) == ["20260101-000000-aaaa"]

    everywhere = cleanup.plan(api, everywhere=True, now=NOW, alive=nobody)
    assert sorted(ids(everywhere.remove)) == ["20260101-000000-aaaa", "20260102-000000-bbbb"]
    assert {c.where for c in everywhere.remove} == {str(api), str(web)}


def test_removing_deletes_the_runs_and_the_emptied_project(project, dai_home):
    record(project, "20260101-000000-aaaa")
    (dai_home / "config.toml").write_text("[roles]\n")

    chosen = cleanup.plan(project, now=NOW, alive=nobody)
    freed, failures = cleanup.remove(chosen)

    assert failures == []
    assert freed == chosen.size
    assert not home.project_dir(project).exists()
    assert (dai_home / "config.toml").read_text() == "[roles]\n"
    assert (dai_home / "projects").is_dir(), "never the projects directory itself"


def test_a_kept_run_keeps_its_project(project):
    record(project, "20260101-000000-aaaa")
    record(project, "20260927-000000-bbbb", finish=False, pid=4242)

    chosen = cleanup.plan(project, now=NOW, alive=lambda pid: True)
    cleanup.remove(chosen)

    assert run_dir(project, "20260927-000000-bbbb").is_dir()
    assert not run_dir(project, "20260101-000000-aaaa").exists()


def test_the_old_layout_in_the_workdir_is_cleaned_too(project):
    old = project / ".dai" / "runs" / "20250101-000000-aaaa"
    old.mkdir(parents=True)
    (old / "events.jsonl").write_text('{"kind": "finish", "outcome": "consensus"}\n')

    chosen = cleanup.plan(project, now=NOW, alive=nobody)

    assert ids(chosen.remove) == ["20250101-000000-aaaa"]
    assert chosen.remove[0].legacy
    cleanup.remove(chosen)
    assert not (project / ".dai").exists()


def test_the_old_dai_directory_goes_only_if_nothing_else_is_in_it(project):
    old = project / ".dai" / "runs" / "20250101-000000-aaaa"
    old.mkdir(parents=True)
    (old / "events.jsonl").write_text('{"kind": "finish", "outcome": "consensus"}\n')
    (project / ".dai" / "notes.txt").write_text("mine")

    cleanup.remove(cleanup.plan(project, now=NOW, alive=nobody))

    assert (project / ".dai" / "notes.txt").read_text() == "mine"
    assert not (project / ".dai" / "runs").exists()


def test_a_symlink_among_the_runs_is_never_followed(project, tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    runs = home.project_dir(project) / "runs"
    runs.mkdir(parents=True)
    (runs / "20250101-000000-aaaa").symlink_to(precious)

    chosen = cleanup.plan(project, now=NOW, alive=nobody)
    cleanup.remove(chosen)

    assert chosen.remove == []
    assert (precious / "keep.txt").exists()


def test_a_run_that_cannot_be_deleted_is_reported_not_raised(project, monkeypatch):
    record(project, "20260101-000000-aaaa")
    record(project, "20260201-000000-bbbb")

    real = cleanup.shutil.rmtree

    def stubborn(path, *args, **kwargs):
        if Path(path).name == "20260101-000000-aaaa":
            raise PermissionError(13, "Permission denied")
        real(path, *args, **kwargs)

    monkeypatch.setattr(cleanup.shutil, "rmtree", stubborn)
    chosen = cleanup.plan(project, now=NOW, alive=nobody)
    freed, failures = cleanup.remove(chosen)

    assert [p.name for p, _ in failures] == ["20260101-000000-aaaa"]
    assert freed == next(c.size for c in chosen.remove if c.run_id.endswith("bbbb"))
    assert run_dir(project, "20260101-000000-aaaa").is_dir()


def test_nothing_recorded_is_nothing_to_do(project, dai_home):
    chosen = cleanup.plan(project, now=NOW, alive=nobody)

    assert (chosen.remove, chosen.running, chosen.newer) == ([], [], 0)
    assert cleanup.remove(chosen) == (0, [])
    assert not dai_home.exists()


def test_a_directory_with_no_log_says_it_was_never_a_run(project):
    images = run_dir(project, "20260101-000000-aaaa") / "images"
    images.mkdir(parents=True)
    age(images.parent, LONG_AGO)

    (candidate,) = cleanup.plan(project, now=NOW, alive=nobody).remove

    assert candidate.recorded is False
