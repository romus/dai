"""The one place the TUI's colours are decided.

Textual's stock themes follow the terminal's own palette, which makes an app
look like a different program on every machine. `dai` keeps its own two — a
dark one and a light one, the same design rendered on ink or on paper — and
picks whichever matches the terminal it was started in. `dai.tui.appearance`
does the picking; this module owns what the two look like.

Both are registered with the app and one is selected, so the CSS can talk in
`$dai-*` variables and the Rich `Text` the widgets assemble can use the same
values as plain style strings.

Nothing here is a constant any more. `theme.MUTED` and `theme.S_ERROR` are
resolved against the *active* palette on every access (see `__getattr__`),
because the terminal's theme can change in the middle of a run and a colour
read at import time would never hear about it. Two rules follow:

* Nothing outside this module may store a resolved colour. Anything that has
  to remember a style — a log line kept for a later repaint — remembers the
  *name* and calls `style()` when it draws.
* Inside this module, a bare `MUTED` is a `NameError`, not a colour: module
  `__getattr__` is only consulted for attribute access from elsewhere, never
  for a global lookup here. The helpers below call `color()` for that reason.

The letterspacing and keycaps are literal characters, not styling — terminals
have no letter-spacing and no way to draw a rounded box inside one line, so a
spaced-out label is spaced-out text and a keycap is a padded background.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache

from rich.text import Text
from textual.theme import Theme

# --- the palettes ---------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    """Every colour the TUI knows, for one appearance."""

    background: str
    surface: str
    panel: str
    #: The selected row in the `@` list. Dark reuses `panel`; light wants a
    #: warmer tint, since a grey-on-grey highlight vanishes on paper.
    highlight: str
    rule: str
    foreground: str
    muted: str
    solver: str
    critic: str
    success: str
    warning: str
    error: str


DARK = Palette(
    background="#0f1115",
    surface="#171a21",
    panel="#1e222b",
    highlight="#1e222b",
    rule="#262a33",
    foreground="#d5d9e0",
    muted="#6d7480",
    solver="#e0a458",
    critic="#7fa6d0",
    success="#7cb87c",
    warning="#e0b25a",
    error="#d97066",
)

#: Warm paper rather than white. The grounds mirror the dark palette, but the
#: accents are not simply the same hues: a colour bright enough to carry on ink
#: is washed out on paper, so each is darkened until it reads at roughly the
#: contrast its dark counterpart has (4.6–5.7 against the background, with
#: `muted` — which carries the most small text of anything here — at 4.6).
LIGHT = Palette(
    background="#faf8f3",
    surface="#f0ede5",
    panel="#e7e3d9",
    highlight="#ecdfc9",
    rule="#dcd7cb",
    foreground="#2b2a26",
    muted="#757168",
    solver="#9c6024",
    critic="#416d97",
    success="#437c46",
    warning="#8b5c15",
    error="#ac3d33",
)

PALETTES = {"dark": DARK, "light": LIGHT}

#: Which palette the widgets are painting with. Swapped by `use()`.
ACTIVE = DARK


def use(appearance: str) -> None:
    """Make `appearance` the palette every colour lookup resolves against.

    An unknown name falls back to dark rather than raising: a stray value in a
    config file should not be the thing that stops a run.
    """

    global ACTIVE
    ACTIVE = PALETTES.get(appearance, DARK)


# --- asking for a colour --------------------------------------------------

#: Rich style strings for the text the widgets assemble by hand, kept as
#: templates so a name can be stored now and resolved later. Anything a log
#: replays after a theme change has to look up its style at *render* time, not
#: at the time it was written — see `AgentPane.repaint`.
_STYLES = {
    "muted": "{muted}",
    "text": "{foreground}",
    "dim-rule": "{rule}",
    "strong": "bold {foreground}",
    "thinking": "italic {muted}",
    "strong-critic": "bold {critic}",
    "plain-warning": "{warning}",
    "success": "bold {success}",
    "warning": "bold {warning}",
    "error": "bold {error}",
}


def color(name: str) -> str:
    """One bare palette colour, by field name."""

    return getattr(ACTIVE, name)


def style(name: str) -> str:
    """One Rich style string, by the names in `_STYLES`."""

    return _resolve(name, ACTIVE)


@lru_cache(maxsize=None)
def _resolve(name: str, palette: Palette) -> str:
    return _STYLES[name].format(**vars(palette))


#: The spelling every widget uses: `theme.MUTED`, `theme.S_ERROR`. They are not
#: module globals, so each mention goes through `__getattr__` below and gets
#: whatever is active at that moment.
_COLOUR_NAMES = {field.name.upper(): field.name for field in fields(Palette)}
_STYLE_NAMES = {
    "S_MUTED": "muted",
    "S_DIM_RULE": "dim-rule",
    "S_TEXT": "text",
    "S_SUCCESS": "success",
    "S_WARNING": "warning",
    "S_ERROR": "error",
}


def __getattr__(name: str) -> str:
    if field := _COLOUR_NAMES.get(name):
        return color(field)
    if style_name := _STYLE_NAMES.get(name):
        return style(style_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- the Textual themes ---------------------------------------------------


def _theme(name: str, palette: Palette, *, dark: bool) -> Theme:
    return Theme(
        name=name,
        primary=palette.solver,
        secondary=palette.critic,
        accent=palette.solver,
        warning=palette.warning,
        error=palette.error,
        success=palette.success,
        foreground=palette.foreground,
        background=palette.background,
        surface=palette.surface,
        panel=palette.panel,
        dark=dark,
        variables={
            # The stylesheet's own. They belong here rather than at the top of
            # styles.tcss: a `$var:` declared in the source is *appended* to
            # the theme's value rather than replacing it, which happens to look
            # like an override for a single colour and silently corrupts
            # anything else (`$dai-solver 35%` would expand to three tokens).
            "dai-rule": palette.rule,
            "dai-muted": palette.muted,
            "dai-solver": palette.solver,
            "dai-critic": palette.critic,
            # The row the cursor is on, on the merge screen. The same tint the
            # `@` list wears below, but the sheet cannot reach it there without
            # calling itself a block cursor, which it is not.
            "dai-highlight": palette.highlight,
            # Textual's own Footer, dressed as the keycap row in the design.
            "footer-background": "transparent",
            "footer-item-background": "transparent",
            "footer-key-background": palette.panel,
            "footer-key-foreground": palette.foreground,
            "footer-description-background": "transparent",
            "footer-description-foreground": palette.muted,
            # The @ list: a quiet card with one highlighted row.
            "block-cursor-background": palette.highlight,
            "block-cursor-foreground": palette.foreground,
            "block-cursor-text-style": "bold",
            "block-cursor-blurred-background": palette.highlight,
            "block-cursor-blurred-foreground": palette.foreground,
            "block-cursor-blurred-text-style": "bold",
            "input-cursor-background": palette.solver,
            "input-cursor-foreground": palette.background,
            "input-selection-background": f"{palette.solver} 35%",
        },
    )


DARK_THEME = _theme("dai-dark", DARK, dark=True)
LIGHT_THEME = _theme("dai-light", LIGHT, dark=False)

THEMES = {"dark": DARK_THEME, "light": LIGHT_THEME}


def theme_name(appearance: str) -> str:
    """The Textual theme name for an appearance."""

    return THEMES.get(appearance, DARK_THEME).name


def apply_theme(app, appearance: str = "dark") -> None:
    """Register both palettes and select one.

    Called from `__init__`, not `on_mount`: the stylesheet is parsed before the
    app mounts, and it would fail on the first `$dai-…` it met.
    """

    use(appearance)
    app.register_theme(DARK_THEME)
    app.register_theme(LIGHT_THEME)
    app.theme = theme_name(appearance)


# --- text the terminal cannot style ---------------------------------------


def spaced(label: str) -> str:
    """`TASK` -> `T A S K`. Letterspacing, the only way a terminal has it."""

    return " ".join(label)


def keycap(text: Text, key: str) -> Text:
    """Append a key as a padded block, so it reads as a key and not as prose."""

    text.append(f" {key} ", style=f"bold {color('foreground')} on {color('panel')}")
    return text


def hint(*pairs: tuple[str, str]) -> Text:
    """A row of `keycap + what it does`, separated by dots."""

    line = Text()
    for index, (key, what) in enumerate(pairs):
        if index:
            line.append("  ·  ", style=color("rule"))
        keycap(line, key)
        line.append(f" {what}", style=color("muted"))
    return line


def meter(done: int, total: int, width: int = 12) -> Text:
    """A two-tone progress bar: how far through the rounds we are."""

    filled = 0 if total <= 0 else max(1, round(width * min(done, total) / total))
    bar = Text()
    bar.append("━" * filled, style=color("solver"))
    bar.append("━" * (width - filled), style=color("rule"))
    return bar
