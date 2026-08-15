"""Snapshots must be recoverable and completely invisible to the user's git state."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dai.config import SnapshotConfig
from dai.snapshot import Snapshotter, find_repos, git, toplevel


def run(*args: str, cwd: Path) -> str:
    """git stdout, empty on failure.

    Same trap the implementation has to dodge: `rev-parse HEAD` on an unborn
    branch exits non-zero while echoing "HEAD" on stdout.
    """

    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def make_repo(path: Path, files: dict[str, str] | None = None, commit: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main", cwd=path)
    run("config", "user.email", "t@example.com", cwd=path)
    run("config", "user.name", "test", cwd=path)
    for name, content in (files or {"a.txt": "one\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if commit:
        run("add", "-A", cwd=path)
        run("commit", "-qm", "initial", cwd=path)
    return path


def state(repo: Path) -> dict:
    """Everything about a repo a user would notice changing."""

    return {
        "head": run("rev-parse", "HEAD", cwd=repo),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo),
        "status": run("status", "--porcelain", cwd=repo),
        "staged": run("diff", "--cached", "--name-only", cwd=repo),
        "stashes": run("stash", "list", cwd=repo),
    }


# --- discovery ------------------------------------------------------------


def test_finds_the_repository_it_is_started_in(tmp_path):
    repo = make_repo(tmp_path / "proj")

    assert find_repos(repo) == [repo.resolve()]


def test_finds_several_repositories_side_by_side(tmp_path):
    make_repo(tmp_path / "api")
    make_repo(tmp_path / "web")

    found = {p.name for p in find_repos(tmp_path)}

    assert found == {"api", "web"}


def test_finds_a_repository_nested_inside_another(tmp_path):
    outer = make_repo(tmp_path / "outer")
    make_repo(outer / "vendor" / "inner")

    found = {p.name for p in find_repos(outer)}

    assert found == {"outer", "inner"}


def test_a_directory_without_git_yields_nothing(tmp_path):
    (tmp_path / "plain").mkdir()

    assert find_repos(tmp_path / "plain") == []
    assert toplevel(tmp_path / "plain") is None


def test_ignored_directories_are_not_searched(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    make_repo(root / "node_modules" / "dep")
    make_repo(root / "real")

    found = {p.name for p in find_repos(root, ignore=["node_modules"])}

    assert found == {"real"}


def test_scan_depth_is_respected(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    make_repo(root / "a" / "b" / "c" / "deep")

    assert find_repos(root, depth=1) == []
    assert {p.name for p in find_repos(root, depth=4)} == {"deep"}


# --- capture --------------------------------------------------------------


def test_snapshot_is_recoverable(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    (repo / "a.txt").write_text("two\n")
    report = snap.capture("r1")

    assert len(report.taken) == 1
    taken = report.taken[0]
    assert run("cat-file", "-p", f"{taken.commit}:a.txt", cwd=repo) == "two"


def test_capture_leaves_the_users_git_state_untouched(tmp_path):
    """The whole point: your branch, HEAD, index and tree must not move."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", "b.txt": "keep\n"})
    (repo / "a.txt").write_text("edited but unstaged\n")
    (repo / "new.txt").write_text("untracked\n")
    run("add", "b.txt", cwd=repo)  # something deliberately left staged

    before = state(repo)
    Snapshotter(repo, "run1").capture("r1")
    after = state(repo)

    assert before == after
    assert (repo / "a.txt").read_text() == "edited but unstaged\n"
    assert (repo / "new.txt").exists()


def test_snapshots_capture_the_working_tree_not_the_index(tmp_path):
    """Uncommitted, unstaged edits are exactly what we need to be able to recover."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "committed\n"})
    (repo / "a.txt").write_text("unstaged edit\n")

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]

    assert run("cat-file", "-p", f"{taken.commit}:a.txt", cwd=repo) == "unstaged edit"


def test_deletions_are_recorded(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", "gone.txt": "bye\n"})
    (repo / "gone.txt").unlink()

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]
    listed = run("ls-tree", "--name-only", taken.commit, cwd=repo).split()

    assert "gone.txt" not in listed
    assert "a.txt" in listed


def test_a_repository_with_no_commits_can_still_be_snapshotted(tmp_path):
    """Fresh repos have no HEAD to use as a parent — the common case for new work."""

    repo = make_repo(tmp_path / "fresh", {"a.txt": "one\n"}, commit=False)

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]

    assert run("cat-file", "-p", f"{taken.commit}:a.txt", cwd=repo) == "one"
    assert run("rev-list", "--count", taken.commit, cwd=repo) == "1"
    assert run("rev-parse", "HEAD", cwd=repo) == ""  # still unborn


def test_gitignored_files_stay_out_of_snapshots(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", ".gitignore": "secret.env\n"})
    (repo / "secret.env").write_text("TOKEN=hunter2\n")

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]
    listed = run("ls-tree", "--name-only", taken.commit, cwd=repo).split()

    assert "secret.env" not in listed


def test_rounds_can_be_diffed_against_each_other(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture("r1")
    (repo / "a.txt").write_text("two\n")
    snap.capture("r2")

    diff = run("diff", snap.ref_for("r1"), snap.ref_for("r2"), cwd=repo)

    assert "-one" in diff and "+two" in diff


def test_every_repository_in_the_workspace_is_captured(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    make_repo(root / "api", {"api.txt": "a\n"})
    make_repo(root / "web", {"web.txt": "w\n"})
    (root / "notes").mkdir()
    (root / "notes" / "free.txt").write_text("no git here\n")

    report = Snapshotter(root, "run1").capture("r1")

    assert {s.repo.name for s in report.taken} == {"api", "web"}
    assert report.skipped == []


def test_snapshotting_can_be_switched_off(tmp_path):
    repo = make_repo(tmp_path / "proj")
    snap = Snapshotter(repo, "run1", SnapshotConfig(enabled=False))

    assert not snap.active
    assert snap.capture("r1").taken == []


def test_a_broken_repository_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    make_repo(root / "good", {"g.txt": "g\n"})
    broken = root / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere/at/all\n")

    report = Snapshotter(root, "run1").capture("r1")

    assert {s.repo.name for s in report.taken} == {"good"}
    assert any("broken" in note for note in report.skipped)


def test_snapshot_commits_are_attributed_to_dai(tmp_path):
    """Bookkeeping commits must not appear to come from the user."""

    repo = make_repo(tmp_path / "proj")

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]

    assert run("show", "-s", "--format=%an", taken.commit, cwd=repo) == "dai"


# --- keeping our own footprint out of the user's git ----------------------


def test_dai_directory_is_hidden_from_git_locally(tmp_path):
    """`.dai/` must not show up as untracked in the user's `git status`."""

    from dai.snapshot import ignore_locally

    repo = make_repo(tmp_path / "proj")
    (repo / ".dai" / "runs").mkdir(parents=True)
    (repo / ".dai" / "runs" / "events.jsonl").write_text("{}\n")

    assert run("status", "--porcelain", cwd=repo) != ""  # visible before
    assert ignore_locally(repo, ".dai/") is True
    assert run("status", "--porcelain", cwd=repo) == ""  # invisible after


def test_local_ignore_does_not_touch_the_users_gitignore(tmp_path):
    from dai.snapshot import ignore_locally

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", ".gitignore": "*.log\n"})

    ignore_locally(repo, ".dai/")

    assert (repo / ".gitignore").read_text() == "*.log\n"
    assert ".dai/" in (repo / ".git" / "info" / "exclude").read_text()


def test_local_ignore_is_not_written_twice(tmp_path):
    from dai.snapshot import ignore_locally

    repo = make_repo(tmp_path / "proj")

    assert ignore_locally(repo, ".dai/") is True
    assert ignore_locally(repo, ".dai/") is False
    body = (repo / ".git" / "info" / "exclude").read_text()
    assert body.count(".dai/") == 1


def test_ignored_bookkeeping_stays_out_of_snapshots(tmp_path):
    from dai.snapshot import ignore_locally

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    ignore_locally(repo, ".dai/")
    (repo / ".dai").mkdir()
    (repo / ".dai" / "events.jsonl").write_text("{}\n")

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]
    listed = run("ls-tree", "-r", "--name-only", taken.commit, cwd=repo).split()

    assert not any(name.startswith(".dai") for name in listed)


def test_local_ignore_on_a_non_repository_is_harmless(tmp_path):
    from dai.snapshot import ignore_locally

    plain = tmp_path / "plain"
    plain.mkdir()

    assert ignore_locally(plain, ".dai/") is False
