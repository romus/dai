"""Config loading, defaults, and CLI overrides."""

from __future__ import annotations

import pytest

from dai.__main__ import _apply_overrides, _relative, build_parser
from dai.config import DEFAULT_CONFIG_TEXT, Merge, ensure_config, from_dict, load


def test_defaults_apply_when_there_is_no_config(tmp_path):
    cfg = load(tmp_path / "absent.toml")

    assert (cfg.solver, cfg.critic) == ("claude", "codex")
    assert cfg.limits.max_rounds == 5
    assert cfg.deadlock_policy == "critic"
    assert cfg.source is None


def test_the_shipped_default_config_parses_and_matches_the_dataclass_defaults(tmp_path):
    """The annotated file users get must agree with the built-in defaults."""

    path, added = ensure_config(tmp_path / "config.toml")
    cfg = load(path)

    assert added == []  # a fresh file is complete by construction

    assert path.read_text() == DEFAULT_CONFIG_TEXT
    assert (cfg.solver, cfg.critic) == ("claude", "codex")
    assert cfg.limits.max_rounds == 5
    assert cfg.limits.max_usd == 5.0
    assert cfg.deadlock_policy == "critic"
    assert cfg.rigor == "standard"
    assert cfg.no_progress_rounds == 2
    assert cfg.stop_on_minor_only is True
    assert cfg.snapshot.enabled is True
    assert cfg.snapshot.branch_prefix == "dai/"
    assert cfg.snapshot.merge is Merge.ASK
    assert cfg.snapshot.branch_from == "default"
    assert cfg.engine("claude").critic_args == ["--permission-mode", "plan"]
    assert cfg.engine("codex").critic_args == ["--sandbox", "read-only"]


def test_ensure_config_never_clobbers_what_you_have_set(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[roles]\nsolver = 'codex'\n")

    ensure_config(path)

    assert "solver = 'codex'" in path.read_text()
    assert load(path).solver == "codex"


def test_a_config_written_before_a_setting_existed_gets_it_added(tmp_path):
    """Otherwise an option added later simply does not exist for its owner.

    `dai --init` used to print the path and write nothing, so a config from an
    older version stayed frozen at the moment it was created.
    """

    path = tmp_path / "config.toml"
    path.write_text(
        "[snapshot]\n# how it used to work\nenabled = true\nscan_depth = 3\n"
        "\n[roles]\nsolver = 'codex'\n"
    )

    _, added = ensure_config(path)
    cfg = load(path)

    assert "snapshot.merge" in added
    assert "snapshot.branch_from" in added
    assert cfg.snapshot.merge is Merge.ASK
    assert cfg.snapshot.enabled is True
    assert cfg.solver == "codex"  # yours, untouched
    assert "# how it used to work" in path.read_text()  # and so are your comments


def test_topping_up_a_config_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[snapshot]\nenabled = true\n")

    ensure_config(path)
    once = path.read_text()
    _, added = ensure_config(path)

    assert added == []
    assert path.read_text() == once


def test_a_setting_you_already_have_is_not_added_twice(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[snapshot]\nmerge = false\n")

    _, added = ensure_config(path)

    assert "snapshot.merge" not in added
    # The value you set survives, in the spelling you set it in: a config from
    # before there was a third answer is not quietly given one.
    assert load(path).snapshot.merge is Merge.NEVER


def test_a_missing_section_is_added_whole(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[roles]\nsolver = 'codex'\n")

    _, added = ensure_config(path)

    assert "snapshot.merge" in added
    assert load(path).snapshot.branch_from == "default"


def test_a_config_that_does_not_parse_is_left_for_you_to_fix(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[snapshot\nthis is not toml\n")

    _, added = ensure_config(path)

    assert added == []
    assert path.read_text() == "[snapshot\nthis is not toml\n"


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


def test_merging_the_work_back_can_be_switched_off_in_the_config():
    cfg = from_dict({"snapshot": {"merge": False}})

    assert cfg.snapshot.merge is Merge.NEVER
    assert cfg.snapshot.enabled is True  # untouched keys keep their defaults


def test_the_older_boolean_spelling_of_merge_still_says_what_it_used_to():
    """A config written when this was a bool must not change meaning under it."""

    assert from_dict({"snapshot": {"merge": True}}).snapshot.merge is Merge.ALWAYS
    assert from_dict({"snapshot": {"merge": False}}).snapshot.merge is Merge.NEVER


def test_merge_can_defer_to_you():
    assert from_dict({"snapshot": {"merge": "ask"}}).snapshot.merge is Merge.ASK
    assert from_dict({"snapshot": {"merge": "always"}}).snapshot.merge is Merge.ALWAYS
    assert from_dict({"snapshot": {"merge": "never"}}).snapshot.merge is Merge.NEVER


def test_a_merge_setting_nobody_recognises_asks_rather_than_guessing():
    """A typo must not decide on its own to write to a branch of yours."""

    assert from_dict({"snapshot": {"merge": "maybe"}}).snapshot.merge is Merge.ASK
    assert from_dict({"snapshot": {}}).snapshot.merge is Merge.ASK


def test_the_branch_can_be_rooted_somewhere_other_than_the_trunk():
    cfg = from_dict({"snapshot": {"branch_from": "current"}})

    assert cfg.snapshot.branch_from == "current"


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


def test_rigor_defaults_to_standard_and_can_be_raised():
    assert from_dict({}).rigor == "standard"
    assert _apply_overrides(from_dict({}), parse("t", "--rigor", "brutal")).rigor == "brutal"


def test_a_rigor_nobody_recognises_is_read_as_standard():
    """A typo in a config file should cost the run its harshness, not the run."""

    assert from_dict({"critique": {"rigor": "ferocious"}}).rigor == "standard"
    assert from_dict({"critique": {"rigor": "BRUTAL"}}).rigor == "brutal"

    with pytest.raises(SystemExit):  # the flag, unlike the file, refuses outright
        parse("t", "--rigor", "ferocious")


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

    cfg = load(ensure_config(tmp_path / "config.toml")[0])

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
    cfg = load(ensure_config(tmp_path / "config.toml")[0])

    assert cfg.theme == "auto"


# --- where the config lives -----------------------------------------------


def test_the_config_lives_in_the_dai_home(dai_home):
    from dai.config import config_path

    assert config_path() == dai_home / "config.toml"


def test_a_config_from_before_the_move_is_still_read(dai_home):
    from dai.config import legacy_config_path

    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[roles]\nsolver = 'codex'\n")

    cfg = load()

    assert cfg.solver == "codex"
    assert cfg.source == legacy


def test_the_new_config_wins_once_it_exists(dai_home):
    from dai.config import config_path, legacy_config_path

    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[roles]\nsolver = 'codex'\n")
    config_path().parent.mkdir(parents=True)
    config_path().write_text("[roles]\ncritic = 'claude'\n")

    cfg = load()

    assert (cfg.solver, cfg.critic) == ("claude", "claude")
    assert cfg.source == config_path()


def test_a_config_named_on_the_command_line_never_falls_back(tmp_path):
    from dai.config import legacy_config_path

    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[roles]\nsolver = 'codex'\n")

    cfg = load(tmp_path / "absent.toml")

    assert cfg.solver == "claude"
    assert cfg.source is None


def test_migrating_copies_the_old_config_once_and_leaves_it_be(dai_home):
    from dai.config import config_path, legacy_config_path, migrate_legacy

    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[roles]\nsolver = 'codex'\n")

    assert migrate_legacy() == legacy
    assert config_path().read_text() == legacy.read_text()
    assert legacy.exists(), "the old file is the user's to delete"

    config_path().write_text("[roles]\nsolver = 'claude'\n")
    assert migrate_legacy() is None, "an existing config is never overwritten"
    assert "claude" in config_path().read_text()


def test_there_is_nothing_to_migrate_without_an_old_config(dai_home):
    from dai.config import migrate_legacy

    assert migrate_legacy() is None
    assert not dai_home.exists()


def test_init_moves_the_old_config_home_and_tops_it_up(dai_home, capsys):
    from dai.__main__ import main
    from dai.config import config_path, legacy_config_path

    legacy = legacy_config_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[roles]\nsolver = 'codex'\n")

    assert main(["--init"]) == 0

    out = capsys.readouterr().out
    assert f"config: {config_path()}" in out
    assert f"copied from {legacy}" in out
    assert "solver = 'codex'" in config_path().read_text()
    assert load().snapshot.merge is Merge.ASK  # topped up with what it lacked
