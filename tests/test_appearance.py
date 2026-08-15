"""Reading the terminal's colour, and keeping its reports out of the keys."""

from __future__ import annotations

import os

import pytest
from textual import events
from textual.messages import TerminalSupportsSynchronizedOutput

from dai.tui import appearance
from dai.tui.appearance import (
    AppearanceChanged,
    SchemeParser,
    detect,
    driver_class,
    from_environment,
    luminance,
    query_terminal,
    read_color,
)
from dai.tui.theme import DARK, LIGHT

# --- reading a colour -----------------------------------------------------


@pytest.mark.parametrize(
    "reply, expected",
    [
        # XParseColor, at each of the widths terminals actually answer with.
        ("\x1b]11;rgb:0f0f/1111/1515\x1b\\", "dark"),
        ("\x1b]11;rgb:fafa/f8f8/f3f3\x07", "light"),
        ("\x1b]11;rgb:0f/11/15\x07", "dark"),
        ("\x1b]11;rgb:000000/000000/000000\x07", "dark"),
        ("\x1b]11;rgba:ffff/ffff/ffff/ffff\x07", "light"),
        # And the few that answer in plain hex.
        ("\x1b]11;#0f1115\x07", "dark"),
        ("\x1b]11;#faf8f3\x07", "light"),
        # Nothing readable is not a guess, it is a "don't know".
        ("\x1b]11;\x07", None),
        ("", None),
        ("\x1b]11;rgb:zz/zz/zz\x07", None),
    ],
)
def test_read_color(reply, expected):
    assert read_color(reply) == expected


def test_luminance_puts_both_shipped_backgrounds_on_the_right_side():
    """The palettes have to survive their own test."""

    assert read_color(DARK.background) == "dark"
    assert read_color(LIGHT.background) == "light"


def test_luminance_weights_green_hardest():
    assert luminance(0, 1, 0) > luminance(1, 0, 0) > luminance(0, 0, 1)


# --- the environment ------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("15;0", "dark"),
        ("0;15", "light"),
        ("0;default;15", "light"),  # the three-field form some shells set
        ("15;default;0", "dark"),
        ("7;7", "light"),
        ("15;8", "dark"),  # 8 is bright black, and still a dark ground
        ("nonsense", None),
        ("", None),
    ],
)
def test_from_environment(monkeypatch, value, expected):
    monkeypatch.setenv("COLORFGBG", value)
    assert from_environment() == expected


def test_from_environment_says_nothing_when_the_variable_is_absent(monkeypatch):
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert from_environment() is None


# --- deciding -------------------------------------------------------------


def test_an_explicit_setting_never_touches_the_terminal(monkeypatch):
    monkeypatch.setattr(
        appearance, "query_terminal", lambda *a, **k: pytest.fail("asked anyway")
    )

    assert detect("dark") == "dark"
    assert detect("light") == "light"


def test_auto_prefers_the_terminal_over_the_environment(monkeypatch):
    monkeypatch.setattr(appearance, "query_terminal", lambda *a, **k: "light")
    monkeypatch.setenv("COLORFGBG", "15;0")

    assert detect("auto") == "light"


def test_auto_falls_back_to_the_environment_then_to_dark(monkeypatch):
    monkeypatch.setattr(appearance, "query_terminal", lambda *a, **k: None)
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert detect("auto") == "light"

    monkeypatch.delenv("COLORFGBG")
    assert detect("auto") == "dark"


def test_a_terminal_that_cannot_be_opened_is_not_a_failure(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no controlling terminal")

    monkeypatch.setattr(os, "open", refuse)

    assert query_terminal() is None


def test_the_query_stops_reading_once_the_attributes_reply_lands():
    """DA1 is asked second and answered second, so it is the full stop."""

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b]11;rgb:fafa/f8f8/f3f3\x07\x1b[?62;c")
        # Nothing closes the write end, so a read that did not stop at the DA1
        # reply would sit here until the timeout instead of returning at once.
        reply = appearance._read_reply(read_fd, timeout=5.0)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert read_color(reply) == "light"


# --- keeping the reports out of the keys ----------------------------------


def messages(parser: SchemeParser, data: str) -> list:
    return list(parser.feed(data))


def test_a_scheme_report_becomes_an_appearance_and_nothing_else():
    for parameter, expected in (("1", "dark"), ("2", "light")):
        out = messages(SchemeParser(), f"\x1b[?997;{parameter}n")

        assert [type(m) for m in out] == [AppearanceChanged]
        assert out[0].appearance == expected


def test_a_report_split_across_two_reads_is_still_understood():
    parser = SchemeParser()

    assert messages(parser, "\x1b[?99") == []
    out = messages(parser, "7;1n")

    assert [type(m) for m in out] == [AppearanceChanged]
    assert out[0].appearance == "dark"


def test_a_report_between_two_keystrokes_costs_neither_of_them():
    out = messages(SchemeParser(), "a\x1b[?997;1nb")

    assert [m.key for m in out if isinstance(m, events.Key)] == ["a", "b"]
    assert [m.appearance for m in out if isinstance(m, AppearanceChanged)] == ["dark"]


@pytest.mark.parametrize("terminator", ["\x07", "\x1b\\"])
def test_a_late_answer_to_our_own_query_is_read_not_typed(terminator):
    """`detect` gives up after a timeout; a slow terminal answers anyway.

    Without this the reply reaches Textual's parser, which reissues sequences
    it does not know as keypresses — straight into the task prompt.
    """

    out = messages(SchemeParser(), f"\x1b]11;rgb:fafa/f8f8/f3f3{terminator}")

    assert [type(m) for m in out] == [AppearanceChanged]
    assert out[0].appearance == "light"


def test_the_escape_key_is_never_held_back():
    """A bare ESC is a prefix of both our sequences, and also a key.

    Holding it back on the chance that more is coming would strand `escape`,
    which cancels the prompt and dismisses every modal.
    """

    parser = SchemeParser()
    messages(parser, "\x1b")

    assert parser._fragment == ""


def test_a_fragment_that_never_completes_is_given_back_as_keys():
    parser = SchemeParser()

    assert messages(parser, "\x1b[?9") == []
    assert parser._fragment == "\x1b[?9"
    assert list(parser.tick()) == []  # not yet: it may still be completed

    # Stand in for the escape delay having passed.
    parser._fragment_at -= 10
    list(parser.tick())

    assert parser._fragment == ""


def test_a_mode_report_textual_wants_passes_straight_through():
    """`\\x1b[?2026…` starts like ours and is none of our business."""

    out = messages(SchemeParser(), "\x1b[?2026;1$y")

    assert [type(m) for m in out] == [TerminalSupportsSynchronizedOutput]


def test_end_of_input_still_means_end_of_input():
    parser = SchemeParser()
    messages(parser, "x")

    assert messages(parser, "") == []


def test_a_chunk_that_was_only_a_report_does_not_read_as_end_of_input():
    """An empty string is how the parser is told the input is over."""

    parser = SchemeParser()
    messages(parser, "\x1b[?997;1n")

    assert [m.key for m in messages(parser, "z") if isinstance(m, events.Key)] == ["z"]


# --- the driver -----------------------------------------------------------


def test_no_driver_of_ours_when_textual_has_been_told_which_to_use(monkeypatch):
    monkeypatch.setattr(appearance.constants, "DRIVER", "textual.drivers.web_driver")

    assert driver_class() is None


def test_no_driver_of_ours_without_a_terminal(monkeypatch):
    monkeypatch.setattr(appearance.constants, "DRIVER", None)
    monkeypatch.setattr(appearance.sys, "__stdin__", None)

    assert driver_class() is None
