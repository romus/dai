"""Where dai keeps its own things: `~/.dai`, one directory per project."""

from __future__ import annotations

import re
from pathlib import Path

from dai import home


def test_dai_home_moves_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("DAI_HOME", str(tmp_path / "elsewhere"))

    assert home.root() == tmp_path / "elsewhere"
    assert home.project_dir(tmp_path).parent == tmp_path / "elsewhere" / "projects"


def test_it_is_read_on_every_call_not_once(monkeypatch, tmp_path):
    monkeypatch.setenv("DAI_HOME", str(tmp_path / "one"))
    assert home.root() == tmp_path / "one"
    monkeypatch.setenv("DAI_HOME", str(tmp_path / "two"))
    assert home.root() == tmp_path / "two"


def test_without_it_the_home_is_dot_dai_in_the_user_s_home(monkeypatch, tmp_path):
    monkeypatch.delenv("DAI_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert home.root() == tmp_path / ".dai"


def test_a_slug_is_stable_safe_and_never_hidden(tmp_path):
    slug = home.project_slug(tmp_path)

    assert slug == home.project_slug(tmp_path)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", slug)
    assert not slug.startswith(("-", "."))


def test_paths_that_fold_to_the_same_text_still_get_their_own_slug():
    """`--clean` deletes by project: a shared slug would delete somebody else's runs."""

    assert home.project_slug(Path("/a/b-c")) != home.project_slug(Path("/a/b/c"))
    assert home.project_slug(Path("/home/u/проект")) != home.project_slug(
        Path("/home/u/задача")
    )


def test_a_space_never_reaches_the_slug():
    assert " " not in home.project_slug(Path("/tmp/my project"))


def test_a_very_long_path_keeps_its_tail_and_its_identity():
    deep = "/" + "/".join(["segment"] * 60)

    slug = home.project_slug(Path(deep + "/the-project"))

    assert len(slug) <= 89
    assert "the-project" in slug
    assert slug != home.project_slug(Path("/x" + deep + "/the-project"))
