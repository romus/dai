"""The plain-terminal frontend: what it asks before it merges, and when it cannot."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from dai.__main__ import _ask_merge, _settle_merge
from dai.config import Merge, SnapshotConfig
from dai.snapshot import Snapshotter


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main", cwd=path)
    run("config", "user.email", "t@example.com", cwd=path)
    run("config", "user.name", "test", cwd=path)
    (path / "a.txt").write_text("one\n")
    run("add", "-A", cwd=path)
    run("commit", "-qm", "initial", cwd=path)
    return path


def worked_in(tmp_path: Path) -> tuple[Snapshotter, Path]:
    """A repository the agents changed, with the run's work committed on it."""

    repo = make_repo(tmp_path / "proj")
    snap = Snapshotter(repo, "run1", SnapshotConfig(merge=Merge.ASK))
    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)
    snap.capture_final()
    return snap, repo


def at_a_terminal(monkeypatch, *, yes: bool = True) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y" if yes else "n")


def test_saying_yes_merges_every_repository_that_can_go(tmp_path, monkeypatch):
    snap, repo = worked_in(tmp_path)
    at_a_terminal(monkeypatch, yes=True)

    asyncio.run(_settle_merge(snap, Merge.ASK, tmp_path))

    assert snap.summary()[0].merged is True
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"


def test_saying_no_keeps_the_branch_and_says_that_is_what_happened(
    tmp_path, monkeypatch
):
    snap, repo = worked_in(tmp_path)
    at_a_terminal(monkeypatch, yes=False)

    asyncio.run(_settle_merge(snap, Merge.ASK, tmp_path))

    entry = snap.summary()[0]

    assert entry.merged is False
    assert entry.note == "you kept the branch"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_a_stdin_that_cannot_answer_is_read_as_no(tmp_path, monkeypatch):
    """An interrupt or a closed pipe must never be mistaken for consent."""

    snap, repo = worked_in(tmp_path)
    at_a_terminal(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))

    asyncio.run(_settle_merge(snap, Merge.ASK, tmp_path))

    assert snap.summary()[0].merged is False
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_with_no_terminal_nobody_is_asked_and_nothing_is_merged(tmp_path, monkeypatch):
    """Piped or in CI: the question cannot be put, so the answer is not assumed."""

    snap, repo = worked_in(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("asked with nobody to ask")
    )

    asyncio.run(_settle_merge(snap, Merge.ASK, tmp_path))

    entry = snap.summary()[0]

    assert entry.merged is False
    assert entry.note == "no terminal to ask on"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_always_merges_without_putting_a_question(tmp_path, monkeypatch):
    snap, repo = worked_in(tmp_path)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("asked when told not to")
    )

    asyncio.run(_settle_merge(snap, Merge.ALWAYS, tmp_path))

    assert snap.summary()[0].merged is True
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"


def test_a_repository_that_cannot_go_is_never_offered(tmp_path, monkeypatch, capsys):
    """With nothing mergeable there is no question worth putting."""

    snap, repo = worked_in(tmp_path)
    run("update-ref", "refs/heads/main", snap.summary()[0].branch, cwd=repo)
    at_a_terminal(monkeypatch)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("offered work that cannot move")
    )

    chosen = _ask_merge(snap.preview())

    assert chosen == []
    assert "a.txt was edited on main too" in capsys.readouterr().out


def test_the_question_shows_what_would_be_written(tmp_path, monkeypatch, capsys):
    snap, _ = worked_in(tmp_path)
    at_a_terminal(monkeypatch, yes=False)

    _ask_merge(snap.preview())
    shown = capsys.readouterr().out

    assert "1 repository changed" in shown
    assert ". → main" in shown
    assert "+1" in shown and "-1" in shown
    assert "a.txt" in shown


# --- dai --clean, --runs, --show ------------------------------------------


def recorded(workdir: Path, run_id: str, task: str = "fix the parser") -> Path:
    from dai.transcript import Transcript
    from test_transcript import sample_result

    workdir.mkdir(parents=True, exist_ok=True)
    t = Transcript(workdir, run_id)
    t.start(task=task, cwd=workdir, solver="claude", critic="codex")
    t.finish(sample_result(), task=task, cwd=workdir, solver="claude", critic="codex")
    return t.dir


def headless(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)


def test_clean_with_yes_deletes_this_directory_s_runs(tmp_path, monkeypatch, capsys):
    from dai.__main__ import main

    headless(monkeypatch)
    run = recorded(tmp_path / "proj", "20260101-000000-aaaa")
    other = recorded(tmp_path / "other", "20260101-000000-bbbb")

    assert main(["--clean", "--yes", "-C", str(tmp_path / "proj")]) == 0

    out = capsys.readouterr().out
    assert "20260101-000000-aaaa" in out and "removed 1 run" in out
    assert not run.exists()
    assert other.exists(), "another directory's runs are not this one's to delete"


def test_a_dry_run_lists_and_deletes_nothing(tmp_path, monkeypatch, capsys):
    from dai.__main__ import main

    headless(monkeypatch)
    run = recorded(tmp_path, "20260101-000000-aaaa")

    assert main(["--clean", "--dry-run", "-C", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "20260101-000000-aaaa" in out and "fix the parser" in out
    assert "nothing deleted" in out
    assert run.exists()


def test_with_nobody_to_ask_and_no_yes_nothing_is_deleted(tmp_path, monkeypatch, capsys):
    from dai.__main__ import main

    headless(monkeypatch)
    run = recorded(tmp_path, "20260101-000000-aaaa")

    assert main(["--clean", "-C", str(tmp_path)]) == 2

    assert "--yes" in capsys.readouterr().err
    assert run.exists()


@pytest.mark.parametrize("answer, survives", [("n", True), ("", True), ("y", False)])
def test_at_a_terminal_it_asks_first(tmp_path, monkeypatch, capsys, answer, survives):
    from dai.__main__ import main

    run = recorded(tmp_path, "20260101-000000-aaaa")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    asked = []
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or answer)

    assert main(["--clean", "-C", str(tmp_path)]) == 0

    assert asked and "[y/N]" in asked[0]
    assert run.exists() is survives


def test_cleaning_nothing_is_not_an_error(tmp_path, monkeypatch, capsys, dai_home):
    from dai.__main__ import main

    headless(monkeypatch)

    assert main(["--clean", "-C", str(tmp_path)]) == 0
    assert "nothing to clean" in capsys.readouterr().out
    assert not dai_home.exists()


def test_a_directory_that_no_longer_exists_can_still_be_cleaned(tmp_path, monkeypatch):
    import shutil

    from dai.__main__ import main

    headless(monkeypatch)
    gone = tmp_path / "gone"
    run = recorded(gone, "20260101-000000-aaaa")
    shutil.rmtree(gone)

    assert main(["--clean", "--yes", "-C", str(gone)]) == 0
    assert not run.exists()


def test_clean_all_reaches_every_directory(tmp_path, monkeypatch):
    from dai.__main__ import main

    headless(monkeypatch)
    runs = [recorded(tmp_path / name, f"20260101-000000-{name}") for name in ("aaaa", "bbbb")]

    assert main(["--clean", "--all", "--yes", "-C", str(tmp_path)]) == 0
    assert not any(run.exists() for run in runs)


def test_the_clean_flags_are_refused_on_their_own():
    from dai.__main__ import main

    for argv in (["--older-than", "3"], ["--yes"], ["--all"], ["--clean", "--older-than", "-1"],
                 ["--clean", "--older-than", "soon"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2


def test_older_than_takes_its_value_and_leaves_the_task_alone():
    from dai.__main__ import build_parser

    args = build_parser().parse_args(["--clean", "--older-than", "7", "t"])

    assert args.older_than == 7.0
    assert args.task == "t"


def test_runs_lists_this_directory_and_all_lists_everywhere(tmp_path, capsys):
    from dai.__main__ import main

    recorded(tmp_path / "api", "20260101-000000-aaaa", "api work")
    recorded(tmp_path / "web", "20260102-000000-bbbb", "web work")

    assert main(["--runs", "-C", str(tmp_path / "api")]) == 0
    here = capsys.readouterr().out
    assert "api work" in here and "web work" not in here

    assert main(["--runs", "--all", "-C", str(tmp_path / "api")]) == 0
    everywhere = capsys.readouterr().out
    assert everywhere.index("web work") < everywhere.index("api work")


def test_runs_mentions_the_old_layout_it_no_longer_lists(tmp_path, capsys):
    from dai.__main__ import main

    (tmp_path / ".dai" / "runs" / "20250101-000000-aaaa").mkdir(parents=True)

    assert main(["--runs", "-C", str(tmp_path)]) == 0
    assert "dai --clean" in capsys.readouterr().out


def test_show_finds_a_run_from_any_directory(tmp_path, capsys):
    from dai.__main__ import main

    recorded(tmp_path / "api", "20260101-000000-aaaa")
    (tmp_path / "elsewhere").mkdir()

    assert main(["--show", "20260101-000000-aaaa", "-C", str(tmp_path / "elsewhere")]) == 0
    assert "# dai run 20260101-000000-aaaa" in capsys.readouterr().out

    assert main(["--show", "../../etc", "-C", str(tmp_path)]) == 2
