"""Developer tools for looking at the TUI without waiting for a real argument.

**This package is not in the built wheel.** `pyproject.toml` excludes it from
the wheel target, while an editable install is a bare `sys.path` entry that the
exclude cannot reach — so it is here when you work from the checkout and gone
from anything `pipx install dai` produces. `__main__` imports it behind a
`try/except ImportError`, so a released build never even grows the flags.

The surface `__main__` touches is two functions, and nothing else:

    add_arguments(parser)   declare the dev flags
    handle(args)            run whatever was asked for; None if nothing was

Importing the package also registers the fake engine, so `dai --solver fake
--critic fake "task"` works on its own without going through `--demo`.
"""

from __future__ import annotations

import argparse

from dai.dev.engine import FakeEngine
from dai.dev.run import demo
from dai.dev.screens import NAMES, preview
from dai.dev.scenarios import SCENARIOS, describe
from dai.engines import ENGINES

ENGINES["fake"] = FakeEngine

__all__ = ["FakeEngine", "add_arguments", "handle"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the dev flags. Only ever called when this package exists."""

    group = parser.add_argument_group("developer (not in released builds)")
    group.add_argument(
        "--preview", choices=NAMES, metavar="SCREEN",
        help=f"open one screen with fake data: {', '.join(NAMES)}",
    )
    # `--demo` is a switch with a separate `--scenario` rather than taking the
    # name itself, for the reason `build_parser` already gives about `--merge`:
    # the task is a positional, and an optional-valued flag would swallow it.
    group.add_argument(
        "--demo", action="store_true", help="run the whole TUI on fake agents"
    )
    group.add_argument(
        "--scenario", choices=sorted(SCENARIOS), metavar="NAME",
        help="which canned argument --demo plays; --scenarios lists them",
    )
    group.add_argument(
        "--scenarios", action="store_true", help="list the canned arguments and exit"
    )


def handle(args) -> int | None:
    """Run whatever dev mode was asked for. None means none was."""

    if getattr(args, "scenarios", False):
        print("scenarios for --demo:")
        for line in describe():
            print(line)
        return 0
    if name := getattr(args, "preview", None):
        preview(name, cwd=_cwd(args), appearance=_appearance(args))
        return 0
    if getattr(args, "demo", False):
        return demo(
            scenario=getattr(args, "scenario", None) or "agree",
            cwd=args.cwd,
            appearance=_appearance(args),
            no_tui=getattr(args, "no_tui", False),
        )
    return None


def _cwd(args):
    from pathlib import Path

    return (args.cwd or Path.cwd()).resolve()


def _appearance(args) -> str:
    from dai.tui.appearance import detect

    return detect(getattr(args, "theme", None) or "auto")
