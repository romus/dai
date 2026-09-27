"""The system clipboard, and the `[ImgN]` tokens a pasted screenshot becomes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dai.protocol import attachments_rule
from dai.transcript import run_dir
from dai.tui import clipboard
from dai.tui.clipboard import Attachments


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


def attachments(workspace: Path, run_id: str = "20260818-120000-abcd") -> Attachments:
    return Attachments(workspace, run_dir(workspace, run_id) / "images")


def pretend(monkeypatch, *, image: bool = False, text: str | None = None) -> None:
    """Stand in for the OS clipboard. The real one is never touched in tests."""

    async def has_image() -> bool:
        return image

    async def save_image(dest: Path) -> bool:
        if not image:
            return False
        dest.write_bytes(b"\x89PNG\r\n\x1a\n")
        return True

    async def read_text() -> str | None:
        return text

    monkeypatch.setattr(clipboard, "has_image", has_image)
    monkeypatch.setattr(clipboard, "save_image", save_image)
    monkeypatch.setattr(clipboard, "read_text", read_text)


# --- the platform end -----------------------------------------------------


async def test_a_missing_helper_is_an_empty_clipboard_not_a_crash():
    assert await clipboard._run(["dai-no-such-binary-anywhere"]) is None


async def test_an_unsupported_platform_simply_has_nothing_on_it(monkeypatch):
    monkeypatch.setattr(sys, "platform", "sunos5")
    assert await clipboard.has_image() is False
    assert await clipboard.save_image(Path("/nowhere/x.png")) is False
    assert await clipboard.read_text() is None


async def test_the_macos_check_never_reads_the_image_into_memory(monkeypatch):
    """It wants the exit code; the data would be megabytes of hex on stdout."""

    seen = {}

    async def fake(argv, *, capture=False):
        seen["argv"] = argv
        seen["capture"] = capture
        return 0, b""

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(clipboard, "_run", fake)

    assert await clipboard.has_image() is True
    assert seen["capture"] is False


async def test_the_macos_save_fetches_the_data_before_opening_the_file(monkeypatch):
    """The other order leaves a zero-byte file behind on a text clipboard."""

    seen = {}

    async def fake(argv, *, capture=False):
        seen["argv"] = argv
        return 1, b""

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(clipboard, "_run", fake)

    assert await clipboard.save_image(Path("/tmp/x.png")) is False
    script = seen["argv"]
    assert script.index("set png_data to (the clipboard as «class PNGf»)") < next(
        i for i, part in enumerate(script) if part.startswith("set fp to open")
    )


async def test_a_quote_in_the_path_cannot_escape_the_applescript_string():
    literal = clipboard._applescript_path(Path('/tmp/we"ird/x.png'))
    assert literal == '"/tmp/we\\"ird/x.png"'


# --- the bookkeeping ------------------------------------------------------


async def test_pasting_writes_the_picture_and_hands_back_a_token(
    monkeypatch, workspace
):
    pretend(monkeypatch, image=True)
    box = attachments(workspace)

    assert await box.grab() == "[Img1]"
    written = box.images_dir / "img1.png"
    assert written.is_file()


async def test_the_token_resolves_to_the_absolute_path_under_the_dai_home(
    monkeypatch, workspace, dai_home
):
    pretend(monkeypatch, image=True)
    box = attachments(workspace)
    await box.grab()

    resolved = box.resolve("look at [Img1] and fix the header")
    picture = box.images_dir / "img1.png"
    assert picture.is_relative_to(dai_home)
    assert resolved == f"look at {picture} and fix the header"
    # A path nobody tells the agents to open is worse than none.
    assert attachments_rule(resolved) != ""


async def test_a_run_directory_inside_the_workspace_is_named_relative_to_it(
    monkeypatch, workspace
):
    """Only if somebody has put `DAI_HOME` in the project — but then relative."""

    pretend(monkeypatch, image=True)
    box = Attachments(workspace, workspace / "shots" / "images")
    await box.grab()

    assert box.resolve("[Img1]") == "shots/images/img1.png"


async def test_a_token_nobody_minted_is_left_alone(monkeypatch, workspace):
    pretend(monkeypatch, image=True)
    box = attachments(workspace)
    await box.grab()

    # `[Img7]` is the user's own typing, not one of ours.
    assert box.resolve("[Img7] and [Img1]") == f"[Img7] and {box.images_dir / 'img1.png'}"


async def test_numbering_counts_off_the_directory_not_off_the_widget(
    monkeypatch, workspace
):
    """The startup prompt and every inject screen build their own registry."""

    pretend(monkeypatch, image=True)
    first = attachments(workspace)
    assert await first.grab() == "[Img1]"

    second = attachments(workspace)
    assert await second.grab() == "[Img2]"

    assert sorted(p.name for p in second.images_dir.iterdir()) == [
        "img1.png",
        "img2.png",
    ]


async def test_a_pasted_picture_leaves_the_user_s_repo_untouched(
    monkeypatch, workspace
):
    """Nothing lands in the project, so there is nothing to hide from git."""

    git("init", "-q", "-b", "main", cwd=workspace)
    exclude = workspace / ".git" / "info" / "exclude"
    before = exclude.read_text() if exclude.exists() else ""
    pretend(monkeypatch, image=True)

    await attachments(workspace).grab()

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True
    ).stdout
    assert status == ""
    assert not (workspace / ".dai").exists()
    assert (exclude.read_text() if exclude.exists() else "") == before


async def test_a_clipboard_with_no_picture_creates_no_directory(
    monkeypatch, workspace
):
    pretend(monkeypatch, image=False)
    box = attachments(workspace)

    assert await box.grab() is None
    assert not box.images_dir.exists()


async def test_a_failed_write_leaves_neither_token_nor_file(monkeypatch, workspace):
    async def has_image() -> bool:
        return True

    async def save_image(dest: Path) -> bool:
        return False

    monkeypatch.setattr(clipboard, "has_image", has_image)
    monkeypatch.setattr(clipboard, "save_image", save_image)
    box = attachments(workspace)

    assert await box.grab() is None
    assert list(box.images_dir.iterdir()) == []


async def test_a_workspace_that_cannot_be_written_to_costs_the_paste_not_the_run(
    monkeypatch, workspace
):
    pretend(monkeypatch, image=True)
    box = attachments(workspace)

    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)
    assert await box.grab() is None


async def test_an_image_outside_the_workspace_keeps_its_absolute_path(
    monkeypatch, tmp_path
):
    pretend(monkeypatch, image=True)
    box = Attachments(tmp_path / "proj", tmp_path / "elsewhere" / "images")
    await box.grab()

    resolved = box.resolve("[Img1]")
    assert resolved == str(tmp_path / "elsewhere" / "images" / "img1.png")
