"""Rounds must land as real commits, and change nothing about where the user is."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dai.config import SnapshotConfig
from dai.snapshot import Snapshotter, find_repos, git, list_branches, toplevel


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


def test_a_repository_nothing_changed_in_is_not_touched_at_all(tmp_path):
    """No branch, no commit, no switch — the run was never here."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    before = state(repo)
    snap.capture_gate(1)
    snap.capture_gate(2)
    snap.capture_final()

    assert state(repo) == before
    assert run("branch", "--list", "--format=%(refname:short)", cwd=repo) == "main"
    assert snap.summary() == []


def test_a_repository_with_something_staged_is_left_alone(tmp_path):
    """A staged-then-edited blob lives only in the index, which `read-tree` eats.

    Snapshots record the working tree, so that content is in no commit of ours;
    moving the repo onto a branch would leave it reachable from nothing.
    """

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n", "b.txt": "keep\n"})
    (repo / "b.txt").write_text("staged\n")
    run("add", "b.txt", cwd=repo)
    (repo / "b.txt").write_text("staged, then edited\n")
    snap = Snapshotter(repo, "run1")

    report = snap.capture_gate(1)
    (repo / "a.txt").write_text("the agents got here\n")
    snap.capture_gate(2)

    assert any("staged" in note for note in report.skipped)
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"
    assert run("branch", "--list", "--format=%(refname:short)", cwd=repo) == "main"
    assert snap.summary() == []
    assert run("show", ":b.txt", cwd=repo) == "staged"  # the index survives
    assert (repo / "a.txt").read_text() == "the agents got here\n"  # never reverted


def test_the_files_on_disk_are_never_rewritten(tmp_path):
    """Not `git checkout`, not `git commit`: nothing may move under the agents."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")
    snap.capture_gate(1)

    (repo / "a.txt").write_text("theirs\n")
    (repo / "new.txt").write_text("untracked\n")
    stamps = {p.name: p.stat().st_mtime_ns for p in repo.glob("*.txt")}
    snap.capture_gate(2)
    snap.capture_final()

    assert {p.name: p.stat().st_mtime_ns for p in repo.glob("*.txt")} == stamps
    assert (repo / "a.txt").read_text() == "theirs\n"
    assert (repo / "new.txt").read_text() == "untracked\n"


def test_a_failing_pre_commit_hook_cannot_stop_a_round(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    report = snap.capture_gate(2)

    assert len(report.taken) == 1
    assert report.skipped == []


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
    assert run("rev-parse", "HEAD", cwd=repo) == taken.commit  # born on our branch


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
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("edited\n")
    taken = snap.capture_gate(2).taken[0]

    assert taken.branch == "dai/run1"
    assert "dai/run1" in run("branch", "--format=%(refname:short)", cwd=repo).split()
    assert run("rev-parse", "dai/run1", cwd=repo) == taken.commit


def test_the_first_change_puts_you_on_the_branch(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"

    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"
    assert run("status", "--porcelain", cwd=repo) == ""  # the round is committed


def test_the_branch_is_rooted_on_the_trunk_not_on_where_you_stand(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    trunk = run("rev-parse", "HEAD", cwd=repo)
    run("checkout", "-q", "-b", "side", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert snap.summary()[0].base_branch == "main"
    assert snap.summary()[0].base == trunk


def test_branching_from_current_stays_where_you_are(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    run("checkout", "-q", "-b", "side", cwd=repo)
    snap = Snapshotter(repo, "run1", SnapshotConfig(branch_from="current"))

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert snap.summary()[0].base_branch == "side"


def test_a_trunk_by_another_name_is_still_found(tmp_path):
    """Not every repository calls it `main`."""

    repo = tmp_path / "proj"
    repo.mkdir()
    run("init", "-q", "-b", "trunk", cwd=repo)
    run("config", "user.email", "t@example.com", cwd=repo)
    run("config", "user.name", "test", cwd=repo)
    (repo / "a.txt").write_text("one\n")
    run("add", "-A", cwd=repo)
    run("commit", "-qm", "initial", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert snap.summary()[0].base_branch == "trunk"


def test_a_branch_you_are_ahead_of_is_not_used_as_the_root(tmp_path):
    """Rooting there would fold your own commits into one `baseline`."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    run("checkout", "-q", "-b", "side", cwd=repo)
    (repo / "mine.txt").write_text("my commit\n")
    run("add", "-A", cwd=repo)
    run("commit", "-qm", "mine", cwd=repo)
    mine = run("rev-parse", "HEAD", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert snap.summary()[0].base_branch == "side"
    assert snap.summary()[0].base == mine


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
    assert run("rev-list", "--count", "main..dai/run1", cwd=repo) == "1"


def test_the_baseline_keeps_your_work_in_progress_out_of_the_rounds(tmp_path):
    """The gate for round 1 is the only moment the two are still separable."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    (repo / "wip.txt").write_text("mine\n")  # yours, uncommitted, before the run
    snap.capture_gate(1)
    (repo / "a.txt").write_text("theirs\n")  # the solver's first round
    first = snap.capture_gate(2).taken[0]

    baseline = run("rev-parse", f"{first.commit}^", cwd=repo)

    assert "baseline" in run("show", "-s", "--format=%s", baseline, cwd=repo)
    assert run("show", "--name-only", "--format=", baseline, cwd=repo) == "wip.txt"
    assert run("show", "--name-only", "--format=", first.commit, cwd=repo) == "a.txt"


def test_a_clean_tree_gets_no_baseline_commit(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("two\n")
    snap.capture_gate(2)

    assert run("rev-list", "--count", "main..dai/run1", cwd=repo) == "1"


def test_a_branch_called_dai_forces_a_flat_name(tmp_path):
    """git keeps refs as files, so `dai` and `dai/run1` cannot both exist."""

    repo = make_repo(tmp_path / "proj")
    run("branch", "dai", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    (repo / "a.txt").write_text("edited\n")
    taken = snap.capture_gate(2).taken[0]

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
    (api / "api.txt").write_text("again\n")
    (web / "web.txt").write_text("again\n")
    snap.capture_gate(2)

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
    snap = Snapshotter(repo, "run1", SnapshotConfig(branch_prefix="agents/"))

    snap.capture_gate(1)
    (repo / "a.txt").write_text("edited\n")
    taken = snap.capture_gate(2).taken[0]

    assert taken.branch == "agents/run1"


# --- what the run leaves behind, and where -------------------------------


def round_of(snap, edits: dict, gate: int):
    """One round: the agents change something, then the gate records it."""

    for path, text in edits.items():
        path.write_text(text)
    return snap.capture_gate(gate)


def test_the_summary_names_the_repository_the_commits_landed_in(tmp_path):
    """A branch name on its own does not say which directory to stand in.

    The workspace need not be a repository, and the one that changed need not
    be the one you were looking at — which is how a finished run comes to look
    like it committed nothing at all.
    """

    root = tmp_path / "ws"
    root.mkdir()
    api = make_repo(root / "api", {"api.txt": "a\n"})
    web = make_repo(root / "web", {"web.txt": "w\n"})
    snap = Snapshotter(root, "run1")

    snap.capture_gate(1)
    round_of(snap, {api / "api.txt": "edited\n"}, 2)
    snap.capture_final()

    rows = snap.summary()

    assert [entry.repo for entry in rows] == [api]
    assert rows[0].commits == 1 and rows[0].switched
    assert rows[0].base_branch == "main"
    assert run("branch", "--list", "--format=%(refname:short)", cwd=web) == "main"


def test_the_summary_remembers_where_the_branch_was_rooted(tmp_path):
    """`HEAD..branch` stops being true the moment anything moves."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    root = run("rev-parse", "HEAD", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    round_of(snap, {repo / "a.txt": "three\n"}, 3)

    entry = snap.summary()[0]

    assert entry.base == root
    assert entry.commits == 2
    assert run("rev-list", "--count", f"{entry.base}..{entry.branch}", cwd=repo) == "2"


def test_a_repository_that_was_never_switched_is_not_in_the_summary(tmp_path):
    """A branch we could not write is not an address worth printing."""

    root = tmp_path / "ws"
    root.mkdir()
    good = make_repo(root / "good", {"g.txt": "g\n"})
    broken = root / "broken"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /nowhere/at/all\n")
    empty = root / "empty"
    empty.mkdir()
    run("init", "-q", "-b", "main", cwd=empty)
    snap = Snapshotter(root, "run1")

    snap.capture_gate(1)
    round_of(snap, {good / "g.txt": "edited\n"}, 2)

    assert [entry.repo for entry in snap.summary()] == [good]


def test_a_repository_with_no_history_is_rooted_on_nothing(tmp_path):
    repo = make_repo(tmp_path / "fresh", {"a.txt": "one\n"}, commit=False)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)

    entry = snap.summary()[0]

    assert entry.base == ""
    # Two: the file was already there when the run began, so it is a baseline
    # of its own, and the round that edited it is the second.
    assert entry.commits == 2
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_the_summary_counts_only_the_rounds_that_changed_something(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_gate(3)  # the solver rebutted, changing nothing

    assert snap.summary()[0].commits == 1


# --- merging back into the branch it was rooted on ------------------------


def test_merging_moves_the_base_branch_and_leaves_you_on_it(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n", repo / "added.txt": "new\n"}, 2)
    snap.capture_final()
    merged = snap.merge()

    assert [entry.merged for entry in merged] == [True]
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main"
    assert run("rev-parse", "main", cwd=repo) == run("rev-parse", "dai/run1", cwd=repo)
    assert run("status", "--porcelain", cwd=repo) == ""
    assert (repo / "a.txt").read_text() == "two\n"
    assert (repo / "added.txt").read_text() == "new\n"


def test_the_preview_says_what_merging_would_write(tmp_path):
    """The numbers on the screen are the diff, not the agents' own account."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n", repo / "added.txt": "new\n"}, 2)
    snap.capture_final()

    row = snap.preview()[0]

    assert row.mergeable
    assert row.label == "."  # the workspace itself
    assert row.branch == "dai/run1"
    assert row.base_branch == "main"
    assert sorted(row.files) == ["a.txt", "added.txt"]
    assert (row.added, row.removed) == (2, 1)  # a.txt one for one, added.txt new


def test_the_preview_counts_the_baseline_because_the_merge_carries_it(tmp_path):
    """It has to say what would land, not only what the agents did."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    (repo / "mine.txt").write_text("mine\n")
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()

    row = snap.preview()[0]

    assert "mine.txt" in row.files


def test_the_preview_of_a_repository_with_no_history_diffs_from_nothing(tmp_path):
    """There is no base commit to diff against; every file is simply new."""

    repo = tmp_path / "fresh"
    repo.mkdir()
    run("init", "-q", "-b", "main", cwd=repo)
    run("config", "user.email", "t@example.com", cwd=repo)
    run("config", "user.name", "test", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "one\n"}, 2)
    snap.capture_final()

    row = snap.preview()[0]

    assert row.files == ("a.txt",)
    assert (row.added, row.removed) == (1, 0)


def test_the_preview_asks_whether_each_one_can_go_without_writing_anything(tmp_path):
    """The whole promise of asking first is that asking costs nothing."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    run("update-ref", "refs/heads/main", snap.summary()[0].branch, cwd=repo)
    before = state(repo)

    row = snap.preview()[0]

    assert not row.mergeable
    assert row.refusal == "a.txt was edited on main too"
    assert state(repo) == before  # asked, and nothing moved


def test_only_the_repositories_you_picked_are_merged(tmp_path):
    one = make_repo(tmp_path / "one", {"a.txt": "one\n"})
    two = make_repo(tmp_path / "two", {"b.txt": "one\n"})
    snap = Snapshotter(tmp_path, "run1")

    snap.capture_gate(1)
    round_of(snap, {one / "a.txt": "two\n", two / "b.txt": "two\n"}, 2)
    snap.capture_final()
    snap.merge(only=[one], kept="you kept the branch")

    rows = {entry.repo: entry for entry in snap.summary()}
    picked, passed = rows[one], rows[two]

    assert picked.merged is True
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=one) == "main"
    assert passed.merged is False
    assert passed.note == "you kept the branch"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=two) == "dai/run1"
    assert run("rev-parse", "main", cwd=two) != run("rev-parse", "dai/run1", cwd=two)


def test_a_repository_that_could_not_go_says_so_rather_than_that_you_kept_it(tmp_path):
    """Two different things, and the record must not read them as one."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    run("update-ref", "refs/heads/main", snap.summary()[0].branch, cwd=repo)
    snap.merge(only=(), kept="you kept the branch")

    assert snap.summary()[0].note == "a.txt was edited on main too"


def test_merging_nothing_is_recorded_as_a_decision_not_as_silence(tmp_path):
    """`merge = false` never asked; a declined prompt did. Both leave a branch."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    snap.merge(only=(), kept="you kept the branch")

    entry = snap.summary()[0]

    assert entry.merged is False
    assert entry.note == "you kept the branch"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_merging_targets_the_branch_the_run_was_rooted_on(tmp_path):
    """Not master by fiat: whatever `branch_from` settled on is the target."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    run("checkout", "-q", "-b", "side", cwd=repo)
    snap = Snapshotter(repo, "run1", SnapshotConfig(branch_from="current"))

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    snap.merge()

    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "side"
    assert run("rev-parse", "side", cwd=repo) == run("rev-parse", "dai/run1", cwd=repo)
    assert run("rev-parse", "main", cwd=repo) != run("rev-parse", "side", cwd=repo)


def test_the_run_branch_survives_merging_so_it_can_be_undone(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    base = snap.summary()[0].base
    snap.merge()

    run("reset", "--hard", base, cwd=repo)

    assert (repo / "a.txt").read_text() == "one\n"
    assert run("rev-parse", "--verify", "dai/run1", cwd=repo) != ""


def test_your_work_in_progress_rides_along_as_its_own_commit(tmp_path):
    """Merging is on by default, so the baseline lands on your branch too."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    (repo / "mine.txt").write_text("mine\n")
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    snap.merge()

    subjects = run("log", "--format=%s", cwd=repo).splitlines()

    assert subjects[-1] == "initial"  # yours, from before the run
    assert "baseline" in subjects[-2]  # yours, uncommitted, now committed
    assert run("status", "--porcelain", cwd=repo) == ""
    assert (repo / "mine.txt").read_text() == "mine\n"


def test_merging_is_refused_when_the_base_branch_moved(tmp_path):
    """Fast-forwarding would drop whatever landed there meanwhile."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    # Someone commits on main while we were on the run's branch.
    run("update-ref", "refs/heads/main", snap.summary()[0].branch, cwd=repo)

    merged = snap.merge()

    assert merged[0].merged is False
    # Named, not just "main moved": which file landed is what tells you whether
    # this is a conflict to sit down with or a rename to wave through.
    assert merged[0].note == "a.txt was edited on main too"
    assert run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "dai/run1"


def test_merging_is_refused_when_you_moved_off_the_branch(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    run("checkout", "-q", "--detach", cwd=repo)

    merged = snap.merge()

    assert merged[0].merged is False
    assert "moved off" in merged[0].note


def test_a_repository_that_changed_nothing_is_left_alone(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    api = make_repo(root / "api", {"api.txt": "a\n"})
    web = make_repo(root / "web", {"web.txt": "w\n"})
    snap = Snapshotter(root, "run1")

    snap.capture_gate(1)
    round_of(snap, {api / "api.txt": "edited\n"}, 2)
    snap.capture_final()
    untouched = state(web)

    merged = snap.merge()

    assert [entry.repo for entry in merged] == [api]
    assert state(web) == untouched


def test_merging_never_raises_when_git_stops_working(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    (repo / ".git" / "HEAD").write_text("total nonsense\n")

    merged = snap.merge()

    assert merged[0].merged is False
    assert merged[0].note


# --- finding the branches again -------------------------------------------


def test_branches_are_found_in_every_repository_in_the_workspace(tmp_path):
    """`git branch --list` in the workspace was the bug: it need not be a repo."""

    root = tmp_path / "ws"
    root.mkdir()
    api = make_repo(root / "api", {"api.txt": "a\n"})
    web = make_repo(root / "web", {"web.txt": "w\n"})
    snap = Snapshotter(root, "run1")

    snap.capture_gate(1)
    round_of(snap, {api / "api.txt": "edited\n", web / "web.txt": "edited\n"}, 2)

    found = list_branches(root)

    assert {(info.repo, info.branch) for info in found} == {
        (api, "dai/run1"),
        (web, "dai/run1"),
    }
    assert all(info.commits == 1 for info in found)


def test_a_branch_of_your_own_is_not_mistaken_for_a_run(tmp_path):
    repo = make_repo(tmp_path / "proj")
    run("branch", "feature/x", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "edited\n"}, 2)

    assert [info.branch for info in list_branches(repo)] == ["dai/run1"]


def test_a_flat_fallback_branch_is_listed_too(tmp_path):
    """The name `_point` settles for has to be the name we go looking for."""

    repo = make_repo(tmp_path / "proj")
    run("branch", "dai", cwd=repo)
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "edited\n"}, 2)

    assert [info.branch for info in list_branches(repo)] == ["dai-run1"]


def test_branch_counts_survive_the_merge(tmp_path):
    """Counted by author, not against HEAD: merging must not zero them."""

    repo = make_repo(tmp_path / "proj", {"a.txt": "one\n"})
    snap = Snapshotter(repo, "run1")

    snap.capture_gate(1)
    round_of(snap, {repo / "a.txt": "two\n"}, 2)
    snap.capture_final()
    snap.merge()

    assert [info.commits for info in list_branches(repo)] == [1]


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
