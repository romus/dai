"""The two palettes, and the late resolution that lets them be swapped."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dai.tui import theme
from dai.tui.theme import DARK, LIGHT, Palette

TUI = Path(__file__).parent.parent / "src" / "dai" / "tui"


@pytest.fixture(autouse=True)
def back_to_dark():
    """`use` is process-wide, so no test may leave the palette swapped."""

    yield
    theme.use("dark")


# --- swapping -------------------------------------------------------------


def test_the_familiar_names_follow_the_active_palette():
    theme.use("light")

    assert theme.MUTED == LIGHT.muted
    assert theme.S_ERROR == f"bold {LIGHT.error}"

    theme.use("dark")

    assert theme.MUTED == DARK.muted
    assert theme.S_ERROR == f"bold {DARK.error}"


def test_an_unknown_appearance_leaves_dai_looking_like_dai():
    theme.use("chartreuse")

    assert theme.ACTIVE is DARK


def test_a_name_that_is_not_a_colour_is_still_an_error():
    with pytest.raises(AttributeError):
        theme.PUCE


def test_each_theme_carries_the_stylesheet_variables():
    """The `$dai-*` the sheet asks for exist in both, or one of them will not load."""

    for textual_theme, palette in (
        (theme.DARK_THEME, DARK),
        (theme.LIGHT_THEME, LIGHT),
    ):
        assert textual_theme.variables["dai-rule"] == palette.rule
        assert textual_theme.variables["dai-muted"] == palette.muted
        assert textual_theme.variables["dai-solver"] == palette.solver
        assert textual_theme.variables["dai-highlight"] == palette.highlight


# --- nothing may ask for a colour that does not exist ---------------------


def used_style_names() -> set[str]:
    """Every style name the TUI passes, gathered from the source.

    A name that is not in `_STYLES` raises only when that line is finally
    drawn — an error path, a rare outcome — so it is worth finding here.
    """

    from dai.tui.app import _OUTCOME_STYLE
    from dai.tui.widgets import _SEVERITY_STYLE

    names = {style for _, style in _OUTCOME_STYLE.values()}
    names |= set(_SEVERITY_STYLE.values())
    for module in ("app.py", "widgets.py"):
        source = (TUI / module).read_text()
        names |= set(re.findall(r'style="([a-z-]+)"', source))
        names |= set(re.findall(r'theme\.style\("([a-z-]+)"\)', source))
    return names


def test_every_style_the_tui_asks_for_is_one_the_theme_has():
    assert used_style_names() <= set(theme._STYLES)


def test_every_phase_colour_is_a_real_palette_field():
    from dai.tui.widgets import _PHASE_COLOR

    fields = {field.name for field in Palette.__dataclass_fields__.values()}

    assert set(_PHASE_COLOR.values()) <= fields


# --- the palettes themselves ----------------------------------------------


def contrast(foreground: str, background: str) -> float:
    def relative(hex_colour: str) -> float:
        channels = []
        for index in (1, 3, 5):
            value = int(hex_colour[index : index + 2], 16) / 255
            channels.append(
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            )
        red, green, blue = channels
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    light, dark = sorted((relative(foreground), relative(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("palette", [DARK, LIGHT], ids=["dark", "light"])
def test_both_palettes_are_readable(palette):
    """A colour picked to look right on ink is washed out on paper.

    The light palette is not the dark one on a pale ground; each accent is
    re-darkened until it carries. This is where that stays true.
    """

    assert contrast(palette.foreground, palette.background) >= 10
    # `muted` carries the most small text of anything here.
    assert contrast(palette.muted, palette.background) >= 4
    for role in ("solver", "critic", "success", "warning", "error"):
        assert contrast(getattr(palette, role), palette.background) >= 4.5, role


@pytest.mark.parametrize("palette", [DARK, LIGHT], ids=["dark", "light"])
def test_the_grounds_stay_quiet(palette):
    """Surface, panel and the hairlines are ground, not figure."""

    for role in ("surface", "panel", "rule", "highlight"):
        assert contrast(getattr(palette, role), palette.background) < 2, role
