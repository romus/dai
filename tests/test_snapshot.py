"""Rounds must land as real commits, and change nothing about where the user is."""

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
    """Where the user is standing — all of it must survive a run untouched.

    Deliberately not a list of branches: giving the run a branch of its own is
    the point of the exercise. What must not move is the branch you are *on*.
    """

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


def test_a_round_is_committed_and_recoverable(tmp_path):
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


def test_commits_capture_the_working_tree_not_the_index(tmp_path):
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


def test_a_repository_with_no_commits_can_still_be_committed(tmp_path):
    """Fresh repos have no HEAD to use as a parent — the common case for new work."""

    repo = make_repo(tmp_path / "fresh", {"a.txt": "one\n"}, commit=False)

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]

    assert run("cat-file", "-p", f"{taken.commit}:a.txt", cwd=repo) == "one"
    assert run("rev-list", "--count", taken.commit, cwd=repo) == "1"
    assert run("rev-parse", "HEAD", cwd=repo) == ""  # still unborn


def test_an_empty_unborn_repository_produces_nothing(tmp_path):
    """`git init` and nothing else is not a round worth a commit."""

    repo = tmp_path / "empty"
    repo.mkdir()
    run("init", "-q", "-b", "main", cwd=repo)

    report = Snapshotter(repo, "run1").capture("r1")

    assert report.taken == []
    assert run("branch", "--list", cwd=repo) == ""


def test_gitignored_files_stay_out_of_commits(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", ".gitignore": "secret.env\n"})
    (repo / "secret.env").write_text("TOKEN=hunter2\n")
    (repo / "a.txt").write_text("two\n")  # something real, or there is no commit

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]
    listed = run("ls-tree", "--name-only", taken.commit, cwd=repo).split()

    assert "secret.env" not in listed


def test_rounds_can_be_diffed_against_each_other(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    branch = snap.branch_for(repo)
    diff = run("diff", f"{branch}~1", branch, cwd=repo)

    assert "-one" in diff and "+two" in diff


def test_every_repository_in_the_workspace_is_captured(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    api = make_repo(root / "api", {"api.txt": "a\n"})
    web = make_repo(root / "web", {"web.txt": "w\n"})
    (root / "notes").mkdir()
    (root / "notes" / "free.txt").write_text("no git here\n")
    (api / "api.txt").write_text("edited\n")
    (web / "web.txt").write_text("edited\n")

    report = Snapshotter(root, "run1").capture("r1")

    assert {s.repo.name for s in report.taken} == {"api", "web"}
    assert report.skipped == []


def test_committing_can_be_switched_off(tmp_path):
    repo = make_repo(tmp_path / "proj")
    snap = Snapshotter(repo, "run1", SnapshotConfig(enabled=False))

    assert not snap.active
    assert snap.capture("r1").taken == []


def test_a_broken_repository_is_skipped_not_fatal(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    good = make_repo(root / "good", {"g.txt": "g\n"})
    broken = root / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere/at/all\n")
    (good / "g.txt").write_text("edited\n")

    report = Snapshotter(root, "run1").capture("r1")

    assert {s.repo.name for s in report.taken} == {"good"}
    assert any("broken" in note for note in report.skipped)


def test_commits_are_attributed_to_dai(tmp_path):
    """dai's commits must not appear to come from the user."""

    repo = make_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("edited\n")

    taken = Snapshotter(repo, "run1").capture("r1").taken[0]

    assert run("show", "-s", "--format=%an", taken.commit, cwd=repo) == "dai"


# --- the run's own branch -------------------------------------------------


def test_the_run_gets_a_branch_of_its_own(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("edited\n")

    snap = Snapshotter(repo, "run1")
    taken = snap.capture_gate(1).taken[0]

    assert taken.branch == "dai/run1"
    assert "dai/run1" in run("branch", "--format=%(refname:short)", cwd=repo).split()
    assert run("rev-parse", "dai/run1", cwd=repo) == taken.commit


def test_the_branch_exists_before_anything_has_changed(tmp_path):
    """You can diff and delete the run's branch from the first round on."""

    repo = make_repo(tmp_path / "proj")
    head = run("rev-parse", "HEAD", cwd=repo)

    Snapshotter(repo, "run1").capture_gate(1)

    assert run("rev-parse", "dai/run1", cwd=repo) == head


def test_rounds_form_a_linear_chain_rooted_at_your_head(tmp_path):
    """Not siblings off HEAD: an ordinary history you can read top to bottom."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    head = run("rev-parse", "HEAD", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    first = snap.capture_gate(2).taken[0]
    (repo / "a.txt").write_text("three\n")
    second = snap.capture_gate(3).taken[0]

    assert run("rev-parse", f"{second.commit}^", cwd=repo) == first.commit
    assert run("rev-parse", f"{first.commit}^", cwd=repo) == head
    assert run("rev-list", "--count", f"{head}..dai/run1", cwd=repo) == "2"


def test_a_round_that_changed_nothing_leaves_no_commit(tmp_path):
    """An argument that spends a round talking should not pad the history."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)
    report = snap.capture_gate(3)  # the solver rebutted, changing nothing

    assert report.taken == []
    assert run("rev-list", "--count", "HEAD..dai/run1", cwd=repo) == "1"


def test_the_baseline_keeps_your_work_in_progress_out_of_the_rounds(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    (repo / "wip.txt").write_text("mine\n")  # yours, uncommitted, before the run
    baseline = snap.capture_gate(1).taken[0]
    (repo / "a.txt").write_text("theirs\n")  # the solver's first round
    first = snap.capture_gate(2).taken[0]

    assert "baseline" in run("show", "-s", "--format=%s", baseline.commit, cwd=repo)
    assert run("show", "--name-only", "--format=", first.commit, cwd=repo) == "a.txt"


def test_a_clean_tree_gets_no_baseline_commit(tmp_path):
    repo = make_repo(tmp_path / "proj")

    report = Snapshotter(repo, "run1").capture_gate(1)

    assert report.taken == []
    assert run("rev-list", "--count", "HEAD..dai/run1", cwd=repo) == "0"


def test_the_branch_can_be_adopted_by_the_users_branch(tmp_path):
    """What the report tells you to run has to actually work.

    A plain `git merge --ff-only` will not: the working tree still holds the
    agents' changes, and git refuses to overwrite them even with their own
    content. `reset --hard` onto a tree the worktree already matches moves the
    branch and rewrites nothing.
    """

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")
    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    (repo / "added.txt").write_text("new\n")
    snap.capture_final()

    run("reset", "--hard", "dai/run1", cwd=repo)

    assert (repo / "a.txt").read_text() == "two\n"
    assert (repo / "added.txt").read_text() == "new\n"
    assert run("status", "--porcelain", cwd=repo) == ""
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"


def test_a_branch_called_dai_forces_a_flat_name(tmp_path):
    """git keeps refs as files, so `dai` and `dai/run1` cannot both exist."""

    repo = make_repo(tmp_path / "proj")
    run("branch", "dai", cwd=repo)
    (repo / "a.txt").write_text("edited\n")

    taken = Snapshotter(repo, "run1").capture_gate(1).taken[0]

    assert taken.branch == "dai-run1"
    assert run("rev-parse", "dai-run1", cwd=repo) == taken.commit


def test_each_repository_gets_its_own_branch(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    api = make_repo(root / "api", {"api.txt": "a\n"})
    web = make_repo(root / "web", {"web.txt": "w\n"})
    (api / "api.txt").write_text("edited\n")
    (web / "web.txt").write_text("edited\n")

    snap = Snapshotter(root, "run1")
    snap.capture_gate(1)

    assert snap.branches == ["dai/run1"]
    for repo in (api, web):
        assert run("rev-parse", "--verify", "dai/run1", cwd=repo) != ""


def test_the_commit_message_names_the_round_and_the_verdict(tmp_path):
    repo = make_repo(tmp_path / "proj")
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("edited\n")
    taken = snap.capture_gate(2, "round 1: 2 issue(s) still open").taken[0]

    message = run("show", "-s", "--format=%B", taken.commit, cwd=repo)

    assert "round 1" in message
    assert "2 issue(s) still open" in message
    assert "run1" in message


def test_the_branch_prefix_is_configurable(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "a.txt").write_text("edited\n")

    snap = Snapshotter(repo, "run1", SnapshotConfig(branch_prefix="agents/"))
    taken = snap.capture_gate(1).taken[0]

    assert taken.branch == "agents/run1"


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


def test_ignored_bookkeeping_stays_out_of_commits(tmp_path):
    from dai.snapshot import ignore_locally

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    ignore_locally(repo, ".dai/")
    (repo / ".dai").mkdir()
    (repo / ".dai" / "events.jsonl").write_text("{}\n")
    snap = Snapshotter(repo, "run1")

    # Our own transcript is not a change to the user's work, so on its own it
    # is not even worth a commit.
    assert snap.capture("r1").taken == []

    (repo / "a.txt").write_text("two\n")
    taken = snap.capture("r2").taken[0]
    listed = run("ls-tree", "-r", "--name-only", taken.commit, cwd=repo).split()

    assert not any(name.startswith(".dai") for name in listed)


def test_local_ignore_on_a_non_repository_is_harmless(tmp_path):
    from dai.snapshot import ignore_locally

    plain = tmp_path / "plain"
    plain.mkdir()

    assert ignore_locally(plain, ".dai/") is False
