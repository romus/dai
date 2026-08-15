"""Configuration: defaults, the TOML file, and the flags that override both."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from dai.budget import Limits, Pricing

APP = "dai"


@dataclass
class EngineConfig:
    """Per-engine command, model, and the flags each role runs under."""

    cmd: str = ""
    model: str = ""
    solve_args: list[str] = field(default_factory=list)
    critic_args: list[str] = field(default_factory=list)

    def args_for(self, *, writing: bool) -> list[str]:
        return list(self.solve_args if writing else self.critic_args)


@dataclass
class SnapshotConfig:
    enabled: bool = True
    scan_depth: int = 3
    ignore: list[str] = field(
        default_factory=lambda: ["node_modules", ".venv", "venv", "target", "dist", "build"]
    )


@dataclass
class Config:
    solver: str = "claude"
    critic: str = "codex"
    limits: Limits = field(default_factory=Limits)
    no_progress_rounds: int = 2
    stop_on_minor_only: bool = True
    deadlock_policy: str = "critic"
    language: str = "auto"
    theme: str = "auto"
    completion_debounce_ms: int = 80
    turn_timeout: float | None = 900.0
    snapshot: SnapshotConfig = field(default_factory=SnapshotConfig)
    engines: dict[str, EngineConfig] = field(default_factory=dict)
    pricing: dict[str, Pricing] = field(default_factory=dict)
    source: Path | None = None

    def engine(self, name: str) -> EngineConfig:
        return self.engines.get(name) or EngineConfig()


def config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / APP


def config_path() -> Path:
    return config_dir() / "config.toml"


DEFAULTS = Config(
    engines={
        "claude": EngineConfig(
            model="opus",
            solve_args=["--permission-mode", "acceptEdits"],
            critic_args=["--permission-mode", "plan"],
        ),
        "codex": EngineConfig(
            solve_args=["--sandbox", "workspace-write"],
            critic_args=["--sandbox", "read-only"],
        ),
    }
)


DEFAULT_CONFIG_TEXT = """\
# dai — two CLI agents argue about your task until they agree.
# Every value here is a default; command-line flags win over it.

[roles]
# The solver does the work and may write. The critic reviews and may not.
solver = "claude"
critic = "codex"

[limits]
max_rounds = 5           # rounds of criticism before the argument is called
max_usd = 5.0            # exact for claude, estimated for codex (see [pricing])
max_wall_seconds = 1800
# max_tokens = 2000000   # a backstop for engines that do not report cost
turn_timeout = 900       # seconds a single agent turn may take

[output]
# Language the agents argue in: "auto" follows the task's own language, or pin
# one so both sides always speak the same way.
language = "auto"

[tui]
# Which palette to wear. "auto" asks the terminal what colour it is and
# follows it, including when you change it mid-run; "dark" and "light" pin one.
theme = "auto"

# How long to wait after a keystroke before refiltering the @ path list.
# 0 filters on every character.
completion_debounce_ms = 80

[consensus]
no_progress_rounds = 2   # identical complaints this many rounds running = deadlock
stop_on_minor_only = true  # stop once nothing worse than `minor` is left open

[deadlock]
# What happens when neither side moves. dai always pauses and shows you the
# disagreement first; this decides what the resolution does.
#   "critic" — the critic's remaining objections are applied, no further rebuttal
#   "solver" — the work stands as the solver left it
#   "ask"    — you decide, in the TUI
policy = "critic"

[snapshot]
# Before each round, dai records the tree of every git repo it finds, under
# refs/dai/<run-id>/r<N>. Your branch, HEAD, index and working tree are never
# touched. Directories without git are skipped.
enabled = true
scan_depth = 3
ignore = ["node_modules", ".venv", "venv", "target", "dist", "build"]

[engines.claude]
# cmd = "claude"
model = "opus"
solve_args = ["--permission-mode", "acceptEdits"]
# `plan` is genuinely read-only, at the cost of a turn spent leaving plan mode.
# To let the critic run checks (tests, linters) instead, swap in:
#   critic_args = ["--permission-mode", "dontAsk",
#                  "--disallowed-tools", "Edit", "Write", "NotebookEdit"]
critic_args = ["--permission-mode", "plan"]

[engines.codex]
# cmd = "codex"
# model = "gpt-5"
solve_args = ["--sandbox", "workspace-write"]
# read-only still allows commands, so this critic can run your tests.
critic_args = ["--sandbox", "read-only"]

# Codex reports tokens but never cost. Without prices here, its spend shows as
# unknown rather than as zero. Fill these in from your provider's pricing.
# [pricing.codex]
# input_per_mtok = 1.25
# output_per_mtok = 10.0
# cache_read_per_mtok = 0.125
"""


def ensure_config(path: Path | None = None) -> Path:
    """Write the annotated default config if the user has none yet."""

    target = path or config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return target


def load(path: Path | None = None) -> Config:
    """Read config from disk, falling back to defaults for anything absent."""

    target = path or config_path()
    if not target.exists():
        return replace(DEFAULTS, source=None)

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    return from_dict(raw, source=target)


def from_dict(raw: dict, *, source: Path | None = None) -> Config:
    roles = raw.get("roles") or {}
    limits_raw = raw.get("limits") or {}
    consensus = raw.get("consensus") or {}
    deadlock = raw.get("deadlock") or {}
    snap = raw.get("snapshot") or {}

    engines = dict(DEFAULTS.engines)
    for name, values in (raw.get("engines") or {}).items():
        base = engines.get(name) or EngineConfig()
        engines[name] = EngineConfig(
            cmd=str(values.get("cmd", base.cmd or "")),
            model=str(values.get("model", base.model)),
            solve_args=_strings(values.get("solve_args"), base.solve_args),
            critic_args=_strings(values.get("critic_args"), base.critic_args),
        )

    pricing = {}
    for name, values in (raw.get("pricing") or {}).items():
        pricing[name] = Pricing(
            input_per_mtok=float(values.get("input_per_mtok", 0) or 0),
            output_per_mtok=float(values.get("output_per_mtok", 0) or 0),
            cache_read_per_mtok=float(values.get("cache_read_per_mtok", 0) or 0),
            cache_write_per_mtok=float(values.get("cache_write_per_mtok", 0) or 0),
        )

    return Config(
        solver=str(roles.get("solver", DEFAULTS.solver)),
        critic=str(roles.get("critic", DEFAULTS.critic)),
        limits=Limits(
            max_rounds=int(limits_raw.get("max_rounds", 5)),
            max_usd=_optional_float(limits_raw.get("max_usd", 5.0)),
            max_tokens=_optional_int(limits_raw.get("max_tokens")),
            max_wall_seconds=_optional_float(limits_raw.get("max_wall_seconds", 1800)),
        ),
        no_progress_rounds=int(consensus.get("no_progress_rounds", 2)),
        stop_on_minor_only=bool(consensus.get("stop_on_minor_only", True)),
        deadlock_policy=str(deadlock.get("policy", "critic")),
        language=str((raw.get("output") or {}).get("language", "auto")),
        theme=_theme(raw.get("tui") or {}),
        # `or 80` would be wrong here: 0 is a real value meaning "no debounce",
        # and it is falsy. Clamped, since a negative delay would mean the list
        # never refreshes at all.
        completion_debounce_ms=_debounce(raw.get("tui") or {}),
        turn_timeout=_optional_float(limits_raw.get("turn_timeout", 900)),
        snapshot=SnapshotConfig(
            enabled=bool(snap.get("enabled", True)),
            scan_depth=int(snap.get("scan_depth", 3)),
            ignore=_strings(snap.get("ignore"), SnapshotConfig().ignore),
        ),
        engines=engines,
        pricing=pricing,
        source=source,
    )


def _theme(tui: dict) -> str:
    # Anything else means "auto": a misspelled palette should leave dai working
    # out what the terminal is, not painting itself in something it invented.
    value = str(tui.get("theme", "auto")).lower()
    return value if value in ("auto", "dark", "light") else "auto"


def _debounce(tui: dict) -> int:
    value = _optional_int(tui.get("completion_debounce_ms"))
    return 80 if value is None else max(0, value)


def _strings(value: object, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    return [str(v) for v in value]


def _optional_float(value: object) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
