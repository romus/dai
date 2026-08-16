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
