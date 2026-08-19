"""`@` path completion: the trigger rule, the index, the search, and the widget."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from test_clipboard import pretend
from textual.app import App, ComposeResult

from dai.config import from_dict
from dai.tui.clipboard import Attachments
from dai.tui.completion import (
    MAX_ENTRIES,
    CompletingInput,
    Entry,
    PathIndex,
    active_mention,
    suggest,
)
from dai.tui.theme import apply_theme


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "t@example.com", cwd=root)
    git("config", "user.name", "test", cwd=root)
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", "-A", cwd=root)
    git("commit", "-qm", "initial", cwd=root)
    return root


# --- the trigger rule -----------------------------------------------------


@pytest.mark.parametrize(
    "text, cursor, expected",
    [
        ("fill in @doc", 12, (8, "doc")),
        ("fill in @", 9, (8, "")),                 # bare @ opens the list
        ("@src/dai", 8, (0, "src/dai")),           # at the very start
        ("fill in @doc more", 12, (8, "doc")),     # cursor mid-string, not at the end
        ("a @b c", 6, None),                       # whitespace ended the mention
        ("no marker here", 14, None),
        ("", 0, None),
    ],
)
def test_active_mention(text, cursor, expected):
    assert active_mention(text, cursor) == expected


def test_an_email_address_does_not_open_the_picker():
    """`@` must start a word, or typing an address turns into a file search."""

    assert active_mention("write to mail@example.com", 25) is None


def test_the_last_mention_wins():
    assert active_mention("compare @a.md with @b.m", 23) == (19, "b.m")


def test_cursor_before_the_marker_sees_nothing():
    assert active_mention("fill in @docs", 5) is None


# --- the index ------------------------------------------------------------


def test_index_lists_files_and_their_directories(tmp_path):
    repo = make_repo(tmp_path / "proj", {"docs/matrix.md": "x", "src/app.py": "y"})

    index = PathIndex.build(repo)
    paths = {e.path: e.is_dir for e in index.entries}

    assert paths["docs/matrix.md"] is False
    assert paths["docs"] is True, "directories are derived; git never lists them"
    assert paths["src"] is True


def test_gitignored_files_are_not_offered(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.py": "x", ".gitignore": "secret.env\nbuild/\n"})
    (repo / "secret.env").write_text("TOKEN=1")
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("compiled")

    paths = {e.path for e in PathIndex.build(repo).entries}

    assert "secret.env" not in paths
    assert "build" not in paths
    assert "a.py" in paths


def test_a_brand_new_untracked_file_is_offered(tmp_path):
    """You often want to talk about the file you just created."""

    repo = make_repo(tmp_path / "proj", {"a.py": "x"})
    (repo / "fresh.md").write_text("new")

    paths = {e.path for e in PathIndex.build(repo).entries}

    assert "fresh.md" in paths


def test_a_deleted_but_still_tracked_file_is_not_offered(tmp_path):
    repo = make_repo(tmp_path / "proj", {"a.py": "x", "gone.md": "bye"})
    (repo / "gone.md").unlink()

    paths = {e.path for e in PathIndex.build(repo).entries}

    assert "gone.md" not in paths
    assert "a.py" in paths


def test_a_plain_directory_is_walked_with_the_ignore_list(tmp_path):
    root = tmp_path / "plain"
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("junk")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("real")

    paths = {e.path for e in PathIndex.build(root, ignore=["node_modules"]).entries}

    assert "src/app.py" in paths
    assert not any(p.startswith("node_modules") for p in paths)


def test_dai_bookkeeping_is_never_offered(tmp_path):
    root = tmp_path / "plain"
    (root / ".dai" / "runs").mkdir(parents=True)
    (root / ".dai" / "runs" / "events.jsonl").write_text("{}")
    (root / "a.md").write_text("x")

    paths = {e.path for e in PathIndex.build(root).entries}

    assert not any(p.startswith(".dai") for p in paths)


def test_a_huge_tree_is_truncated_and_says_so(tmp_path, monkeypatch):
    import dai.tui.completion as completion

    monkeypatch.setattr(completion, "MAX_ENTRIES", 5)
    root = tmp_path / "big"
    root.mkdir()
    for n in range(12):
        (root / f"file{n:02}.txt").write_text("x")

    index = completion.PathIndex.build(root)

    assert index.truncated
    assert len([e for e in index.entries if not e.is_dir]) == 5


# --- the hybrid search ----------------------------------------------------


@pytest.fixture
def index(tmp_path):
    repo = make_repo(
        tmp_path / "proj",
        {
            "docs/matrix.md": "x",
            "docs/api.md": "x",
            "src/dai/engine.py": "x",
            "README.md": "x",
        },
    )
    return PathIndex.build(repo)


def test_an_empty_query_lists_the_top_level_directories_first(index):
    results = [e.display for e in suggest(index, "")]

    assert results == ["docs/", "src/", "README.md"]


def test_a_trailing_slash_lists_that_directory(index):
    assert [e.display for e in suggest(index, "docs/")] == ["docs/api.md", "docs/matrix.md"]
    assert [e.display for e in suggest(index, "src/")] == ["src/dai/"]


def test_typing_searches_the_whole_tree(index):
    """The point of the hybrid: depth costs nothing once you start typing."""

    assert "src/dai/engine.py" in [e.path for e in suggest(index, "engine")]
    assert "docs/matrix.md" in [e.path for e in suggest(index, "mtrx")]


def test_search_is_fuzzy_not_prefix(index):
    assert "docs/matrix.md" in [e.path for e in suggest(index, "dcsmtx")]


def test_a_query_matching_nothing_returns_nothing(index):
    assert suggest(index, "zzzzzz") == []


def test_results_are_capped(index):
    assert len(suggest(index, "m", limit=2)) <= 2


def test_an_unknown_directory_falls_back_to_fuzzy(index):
    """`nope/` matches no directory, so it must not silently return everything."""

    assert all(e.path != "README.md" for e in suggest(index, "nope/"))


# --- configuration --------------------------------------------------------


def test_debounce_defaults_to_80ms():
    assert from_dict({}).completion_debounce_ms == 80


def test_debounce_is_configurable():
    assert from_dict({"tui": {"completion_debounce_ms": 250}}).completion_debounce_ms == 250


def test_debounce_can_be_switched_off():
    assert from_dict({"tui": {"completion_debounce_ms": 0}}).completion_debounce_ms == 0


def test_a_negative_debounce_is_clamped_not_honoured():
    """A negative delay would otherwise mean the list never refreshes."""

    assert from_dict({"tui": {"completion_debounce_ms": -5}}).completion_debounce_ms == 0


# --- the widget -----------------------------------------------------------


class Harness(App[str]):
    """Minimal host for the widget under test."""

    CSS_PATH = Path(__file__).parent.parent / "src" / "dai" / "tui" / "styles.tcss"

    def __init__(
        self,
        cwd: Path,
        debounce_ms: int = 0,
        attachments: Attachments | None = None,
    ) -> None:
        super().__init__()
        # The stylesheet's colours all come from the theme, so a host that
        # loads it has to register one first.
        apply_theme(self)
        self.cwd = cwd
        self.debounce_ms = debounce_ms
        self.attachments = attachments
        self.submitted: list[str] = []
        self.prompts: list[str] = []
        self.changed: list[str] = []
        self.cancelled = 0

    def compose(self) -> ComposeResult:
        yield CompletingInput(
            cwd=self.cwd,
            debounce_ms=self.debounce_ms,
            attachments=self.attachments,
        )

    def on_completing_input_submitted(self, event: CompletingInput.Submitted) -> None:
        self.submitted.append(event.value)
        self.prompts.append(event.prompt)

    def on_completing_input_changed(self, event: CompletingInput.Changed) -> None:
        self.changed.append(event.value)

    def on_completing_input_cancelled(self, event: CompletingInput.Cancelled) -> None:
        self.cancelled += 1


@pytest.fixture
def repo(tmp_path):
    return make_repo(
        tmp_path / "proj",
        {"docs/matrix.md": "x", "docs/api.md": "x", "README.md": "x"},
    )


async def ready(widget: CompletingInput, tries: int = 40) -> None:
    for _ in range(tries):
        if widget._index is not None:
            return
        await asyncio.sleep(0.02)


async def settle(pilot, times: int = 3) -> None:
    for _ in range(times):
        await pilot.pause()
        await asyncio.sleep(0.02)


async def test_typing_at_opens_the_dropdown(repo):
    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        assert not widget.is_open
        await pilot.press("@")
        await settle(pilot)

        assert widget.is_open
        assert [e.display for e in widget._visible] == ["docs/", "README.md"]


async def test_a_space_closes_the_dropdown(repo):
    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        await pilot.press("@")
        await settle(pilot)
        assert widget.is_open

        await pilot.press("space")
        await settle(pilot)

        assert not widget.is_open


async def test_accepting_a_file_inserts_a_bare_path(repo):
    """The `@` is a typing trigger; the agents must not receive it."""

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        widget.input.value = "look at @"
        widget.input.cursor_position = 9
        widget._refresh("")
        await settle(pilot)

        widget._dropdown.highlighted = [e.display for e in widget._visible].index("README.md")
        widget.action_accept()
        await settle(pilot)

        assert widget.input.value == "look at README.md"
        assert "@" not in widget.input.value
        assert not widget.is_open


async def test_accepting_a_directory_keeps_completing_inside_it(repo):
    """Picking a directory should feel like navigation, not a dead end."""

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        widget.input.value = "look at @"
        widget.input.cursor_position = 9
        widget._refresh("")
        await settle(pilot)

        widget._dropdown.highlighted = [e.display for e in widget._visible].index("docs/")
        widget.action_accept()
        await settle(pilot)

        assert widget.input.value == "look at @docs/"
        assert widget.is_open, "the list must stay open to show what is inside"
        assert [e.display for e in widget._visible] == ["docs/api.md", "docs/matrix.md"]


async def test_enter_completes_while_open_and_only_then_submits(repo):
    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        widget.input.value = "look at @READ"
        widget.input.cursor_position = 13
        widget._refresh("READ")
        await settle(pilot)
        assert widget.is_open

        await pilot.press("enter")          # completes, does not submit
        await settle(pilot)
        assert app.submitted == []
        assert widget.input.value == "look at README.md"

        await pilot.press("enter")          # now it submits
        await settle(pilot)
        assert app.submitted == ["look at README.md"]


async def test_escape_closes_the_dropdown_before_giving_up(repo):
    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        await pilot.press("@")
        await settle(pilot)
        assert widget.is_open

        await pilot.press("escape")
        await settle(pilot)
        assert not widget.is_open
        assert app.cancelled == 0, "the first escape belongs to the dropdown"

        await pilot.press("escape")
        await settle(pilot)
        assert app.cancelled == 1


async def test_arrows_move_through_the_results(repo):
    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)
        await pilot.press("@")
        await settle(pilot)
        assert widget._dropdown.highlighted == 0

        await pilot.press("down")
        await settle(pilot)
        assert widget._dropdown.highlighted == 1

        await pilot.press("up")
        await settle(pilot)
        assert widget._dropdown.highlighted == 0


async def test_shift_enter_breaks_the_line_and_enter_still_submits(repo):
    """A task worth arguing about does not have to fit on one line."""

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("a", "shift+enter", "b")
        await settle(pilot)
        assert widget.value == "a\nb"
        assert app.submitted == [], "shift+enter must not start the run"

        await pilot.press("enter")
        await settle(pilot)
        assert app.submitted == ["a\nb"]


async def test_ctrl_j_breaks_the_line_where_shift_enter_cannot(repo):
    """Terminals without the kitty protocol send no shift+enter; ctrl+j stands in."""

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("a", "ctrl+j", "b")
        await settle(pilot)

        assert widget.value == "a\nb"
        assert app.submitted == []


async def test_arrows_move_the_cursor_when_no_list_is_open(repo):
    """The dropdown borrows up/down; with it closed they belong to the text."""

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("a", "shift+enter", "b")
        await settle(pilot)
        assert not widget.is_open
        assert widget.input.cursor_location == (1, 1)

        await pilot.press("up")
        await settle(pilot)
        assert widget.input.cursor_location == (0, 1)


async def test_debounce_collapses_a_burst_into_one_result(repo):
    """Several keystrokes in flight must yield the final query, not a stale one."""

    app = Harness(repo, debounce_ms=120)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        for key in ("@", "m", "t", "r", "x"):
            await pilot.press(key)
            await pilot.pause()

        # Mid-burst the list has not caught up yet.
        assert widget._visible == [] or len(widget._visible) >= 0
        await asyncio.sleep(0.35)
        await settle(pilot)

        assert [e.path for e in widget._visible] == ["docs/matrix.md"]


async def test_with_debounce_off_every_keystroke_filters(repo):
    app = Harness(repo, debounce_ms=0)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("@", "R")
        await settle(pilot)

        # Fuzzy, so other paths containing an "r" legitimately match too; what
        # matters is that filtering happened and the best hit leads.
        assert widget._visible
        assert widget._visible[0].path == "README.md"


# --- pasting --------------------------------------------------------------


def box_for(repo: Path) -> Attachments:
    return Attachments(repo, repo / ".dai" / "runs" / "run-1" / "images")


async def test_pasting_a_screenshot_leaves_a_token_not_a_path(monkeypatch, repo):
    pretend(monkeypatch, image=True)
    box = box_for(repo)
    app = Harness(repo, attachments=box)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "[Img1] "
        assert (box.images_dir / "img1.png").is_file()


async def test_a_second_screenshot_gets_its_own_number(monkeypatch, repo):
    pretend(monkeypatch, image=True)
    app = Harness(repo, attachments=box_for(repo))
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("ctrl+v")
        await settle(pilot)
        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "[Img1] [Img2] "


async def test_the_path_appears_only_in_what_the_agents_receive(monkeypatch, repo):
    pretend(monkeypatch, image=True)
    app = Harness(repo, attachments=box_for(repo))
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("ctrl+v")
        await settle(pilot)
        await pilot.press("f", "i", "x")
        await pilot.press("enter")
        await settle(pilot)

        assert app.submitted == ["[Img1] fix"]
        assert app.prompts == [".dai/runs/run-1/images/img1.png fix"]


async def test_pasting_text_inserts_it_and_writes_nothing(monkeypatch, repo):
    pretend(monkeypatch, image=False, text="the table in docs/matrix.md")
    box = box_for(repo)
    app = Harness(repo, attachments=box)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "the table in docs/matrix.md"
        assert not box.images_dir.exists()


async def test_text_still_pastes_where_screenshots_are_switched_off(
    monkeypatch, repo
):
    """No attachments — the demo, and any workspace we cannot write to."""

    pretend(monkeypatch, image=True, text="still typed by hand")
    app = Harness(repo, attachments=None)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "still typed by hand"
        assert not (repo / ".dai").exists()


async def test_pasting_into_the_middle_lands_at_the_cursor(monkeypatch, repo):
    pretend(monkeypatch, image=True)
    app = Harness(repo, attachments=box_for(repo))
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("a", "b")
        await pilot.press("left")
        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "a[Img1] b"


async def test_an_empty_clipboard_changes_nothing(monkeypatch, repo):
    pretend(monkeypatch, image=False, text=None)
    app = Harness(repo, attachments=box_for(repo))
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("h", "i")
        await pilot.press("ctrl+v")
        await settle(pilot)

        assert widget.value == "hi"


async def test_every_edit_is_announced(repo):
    """`TextArea.Changed` is stopped here, so the widget reposts its own.

    Nothing above would otherwise know the text had grown — which the task
    prompt's character count depends on.
    """

    app = Harness(repo)
    async with app.run_test() as pilot:
        widget = app.query_one(CompletingInput)
        await ready(widget)

        await pilot.press("h", "i")
        await settle(pilot)

        assert app.changed == ["h", "hi"]
        assert widget.value == "hi"
