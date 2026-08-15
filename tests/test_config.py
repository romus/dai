"""Config loading, defaults, and CLI overrides."""

from __future__ import annotations

import pytest

from dai.__main__ import _apply_overrides, _relative, build_parser
from dai.config import DEFAULT_CONFIG_TEXT, ensure_config, from_dict, load


def test_defaults_apply_when_there_is_no_config(tmp_path):
    cfg = load(tmp_path / "absent.toml")

    assert (cfg.solver, cfg.critic) == ("claude", "codex")
    assert cfg.limits.max_rounds == 5
    assert cfg.deadlock_policy == "critic"
    assert cfg.source is None


def test_the_shipped_default_config_parses_and_matches_the_dataclass_defaults(tmp_path):
    """The annotated file users get must agree with the built-in defaults."""

    path = ensure_config(tmp_path / "config.toml")
    cfg = load(path)

    assert path.read_text() == DEFAULT_CONFIG_TEXT
    assert (cfg.solver, cfg.critic) == ("claude", "codex")
    assert cfg.limits.max_rounds == 5
    assert cfg.limits.max_usd == 5.0
    assert cfg.deadlock_policy == "critic"
    assert cfg.no_progress_rounds == 2
    assert cfg.stop_on_minor_only is True
    assert cfg.snapshot.enabled is True
    assert cfg.snapshot.branch_prefix == "dai/"
    assert cfg.snapshot.merge_on_consensus is False
    assert cfg.engine("claude").critic_args == ["--permission-mode", "plan"]
    assert cfg.engine("codex").critic_args == ["--sandbox", "read-only"]


def test_ensure_config_never_clobbers_an_existing_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[roles]\nsolver = 'codex'\n")

    ensure_config(path)

    assert "codex" in path.read_text()
    assert load(path).solver == "codex"


def test_partial_config_keeps_defaults_for_everything_else():
    cfg = from_dict({"roles": {"solver": "codex"}})

    assert cfg.solver == "codex"
    assert cfg.critic == "codex"  # explicit: roles.critic was not overridden
    assert cfg.limits.max_rounds == 5
    assert cfg.engine("claude").critic_args == ["--permission-mode", "plan"]


def test_the_run_branch_can_be_named_something_else():
    cfg = from_dict({"snapshot": {"branch_prefix": "agents/"}})

    assert cfg.snapshot.branch_prefix == "agents/"
    assert cfg.snapshot.scan_depth == 3  # untouched keys keep their defaults


def test_merging_the_work_back_can_be_switched_on_in_the_config():
    cfg = from_dict({"snapshot": {"merge_on_consensus": True}})

    assert cfg.snapshot.merge_on_consensus is True
    assert cfg.snapshot.enabled is True  # untouched keys keep their defaults


def test_engine_overrides_merge_rather_than_replace():
    cfg = from_dict({"engines": {"claude": {"model": "haiku"}}})

    assert cfg.engine("claude").model == "haiku"
    assert cfg.engine("claude").solve_args == ["--permission-mode", "acceptEdits"]


def test_limits_can_be_switched_off():
    cfg = from_dict({"limits": {"max_usd": False, "max_wall_seconds": False}})

    assert cfg.limits.max_usd is None
    assert cfg.limits.max_wall_seconds is None


def test_pricing_is_read_for_engines_that_hide_their_cost():
    cfg = from_dict({"pricing": {"codex": {"input_per_mtok": 1.25, "output_per_mtok": 10.0}}})

    assert cfg.pricing["codex"].known
    assert cfg.pricing["codex"].output_per_mtok == 10.0


def test_engine_args_depend_on_whether_the_role_writes():
    cfg = from_dict({})

    assert cfg.engine("codex").args_for(writing=True) == ["--sandbox", "workspace-write"]
    assert cfg.engine("codex").args_for(writing=False) == ["--sandbox", "read-only"]


# --- CLI overrides --------------------------------------------------------


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_flags_win_over_the_config_file():
    cfg = _apply_overrides(from_dict({}), parse("t", "--solver", "codex", "--critic",
                                                "claude", "--rounds", "9", "--budget", "1.5"))

    assert (cfg.solver, cfg.critic) == ("codex", "claude")
    assert cfg.limits.max_rounds == 9
    assert cfg.limits.max_usd == 1.5


def test_absent_flags_leave_config_values_alone():
    cfg = _apply_overrides(from_dict({"limits": {"max_rounds": 7}}), parse("t"))

    assert cfg.limits.max_rounds == 7
    assert cfg.solver == "claude"


def test_policy_flag_is_constrained_to_known_values():
    assert _apply_overrides(from_dict({}), parse("t", "--policy", "solver")).deadlock_policy == "solver"
    with pytest.raises(SystemExit):
        parse("t", "--policy", "whoever-shouts-loudest")


# --- display --------------------------------------------------------------


def test_paths_are_shown_relative_to_the_working_directory(tmp_path):
    assert _relative(f"{tmp_path}/docs/matrix.md", tmp_path) == "docs/matrix.md"
    assert _relative(str(tmp_path), tmp_path) == "."


def test_long_text_keeps_its_head_not_its_tail(tmp_path):
    shown = _relative("x" * 300, tmp_path, width=20)

    assert len(shown) == 20
    assert shown.endswith("…")


def test_language_defaults_to_auto():
    assert from_dict({}).language == "auto"
    assert load(__import__("pathlib").Path("/nonexistent/dai.toml")).language == "auto"


def test_language_can_be_pinned_in_config():
    assert from_dict({"output": {"language": "Spanish"}}).language == "Spanish"


def test_lang_flag_overrides_the_config():
    cfg = _apply_overrides(from_dict({"output": {"language": "English"}}),
                           parse("t", "--lang", "Spanish"))

    assert cfg.language == "Spanish"


def test_the_shipped_config_documents_the_language_setting(tmp_path):
    from dai.config import ensure_config

    cfg = load(ensure_config(tmp_path / "config.toml"))

    assert cfg.language == "auto"


# --- the palette ----------------------------------------------------------


def test_theme_defaults_to_following_the_terminal():
    assert from_dict({}).theme == "auto"


def test_theme_can_be_pinned_in_config():
    assert from_dict({"tui": {"theme": "light"}}).theme == "light"
    assert from_dict({"tui": {"theme": "DARK"}}).theme == "dark"


def test_an_unknown_theme_falls_back_to_following_the_terminal():
    """A misspelled palette should leave dai working it out, not inventing one."""

    assert from_dict({"tui": {"theme": "purple"}}).theme == "auto"


def test_theme_flag_overrides_the_config():
    cfg = _apply_overrides(from_dict({"tui": {"theme": "dark"}}), parse("t", "--theme", "light"))

    assert cfg.theme == "light"


def test_theme_flag_is_constrained_to_known_values():
    with pytest.raises(SystemExit):
        parse("t", "--theme", "solarized")


def test_the_shipped_config_documents_the_theme_setting(tmp_path):
    cfg = load(ensure_config(tmp_path / "config.toml"))

    assert cfg.theme == "auto"
