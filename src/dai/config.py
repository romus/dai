"""Configuration: defaults, the TOML file, and the flags that override both."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from dai import home
from dai.budget import Limits, Pricing
from dai.protocol import RIGOR, STANDARD

APP = "dai"


class Merge(StrEnum):
    """What consensus does with the run's branch.

    A `StrEnum` rather than three constants so that `Merge.ASK == "ask"` and the
    setting can be compared against what the TOML file literally says, in both
    directions, without a translation table anybody could forget to extend.
    """

    NEVER = "never"
    ASK = "ask"
    ALWAYS = "always"


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
    branch_prefix: str = "dai/"
    #: What the run's branch is rooted on — and so what it merges back into.
    #: "default" finds the trunk, "current" stays where you are, or name one.
    branch_from: str = "default"
    #: Deliberately not a bool any more, and deliberately `ask` by default: the
    #: merge writes to a branch of the user's, and the honest default is the one
    #: that cannot do that without being told to.
    merge: Merge = Merge.ASK
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
    rigor: str = STANDARD
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


def config_path() -> Path:
    return home.root() / "config.toml"


def legacy_config_path() -> Path:
    """Where the config lived before everything moved under `~/.dai`.

    Still read when the new one does not exist, so an upgrade does not quietly
    forget somebody's settings; `dai --init` copies it across.
    """

    root = os.environ.get("XDG_CONFIG_HOME")
    return (Path(root) if root else Path.home() / ".config") / APP / "config.toml"


def migrate_legacy() -> Path | None:
    """Copy the old config into `~/.dai`, once. Returns where it came from.

    A copy, not a move: the old file is the user's, and once the new one exists
    it is simply never read again. Copied byte for byte even if it is broken —
    `ensure_config` refuses to top up a file it cannot parse, and so should this.
    """

    target, legacy = config_path(), legacy_config_path()
    if target.exists() or not legacy.is_file():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(legacy.read_bytes())
    return legacy


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

[critique]
# How hard the two agents lean on each other. The evidence rules are the same at
# every level — an approval always has to name what it examined, and an issue
# always needs a file:line or real command output — this sets how far the critic
# hunts and how hard the solver defends its work.
#   "easy"     — is the task done, is anything broken; nothing beyond that
#   "standard" — every clause of the task, plus one look where the solver did not
#   "strict"   — assume a defect is there and go find it: error paths, callers, tests
#   "brutal"   — hostile review, and a solver told to hold its ground
# Above "standard", expect more rounds and more spend before they agree.
rigor = "standard"

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
# A repo the agents change is moved onto a branch of dai's own, dai/<run-id>,
# and each round lands there as an ordinary commit. Repos nothing changed in
# are not touched at all. Neither the switch nor the commits go through
# `git checkout` or `git commit` — both rewrite files and run your hooks, and a
# formatter firing mid-round would edit the tree under the agents' feet — so no
# file on disk is ever rewritten by dai and no hook of yours fires. Whatever was
# uncommitted before the run is kept as a `baseline` commit of its own.
# `dai --snapshots` lists which repo ended up with what.
enabled = true
scan_depth = 3
branch_prefix = "dai/"

# What the run's branch is rooted on — and therefore what it merges back into.
#   "default" — the repo's trunk: origin/HEAD, else main, master or trunk
#   "current" — the branch you are standing on
#   or name one outright, e.g. "develop"
# If you are ahead of the branch named here, dai roots the run where you are
# instead: folding your own commits into one `baseline` and carrying them back
# on the merge is not something to do quietly.
branch_from = "default"

# On consensus, fast-forward that base branch onto the run's work and leave you
# standing on it, so `git status` is clean and `git log` reads as the work
# having simply been done.
#   true    — merge, without asking
#   false   — never merge; you are left on dai/<run-id>, the work committed
#             there and yours to merge by hand
#   "ask"   — you are shown what each repo would write, and pick which of them
#             go; in the TUI that is a list you tick, and on a plain terminal
#             a y/N question. Piped or redirected, with nobody to ask, nothing
#             is merged.
# A repo whose base branch moved during the run is refused whatever this says,
# with the reason printed, and stays on dai/<run-id>. Only consensus merges: a
# deadlock or a run you killed never does.
merge = "ask"

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


def ensure_config(path: Path | None = None) -> tuple[Path, list[str]]:
    """Write the annotated default config, or top up the one already there.

    Returns the path and whatever was added. A config written a version ago is
    otherwise frozen at the moment it was created: options added since simply
    do not exist for its owner, who has no way to discover them short of
    reading the source. So the missing keys are appended, with the comments
    that explain them, and nothing you have set is touched.
    """

    target = path or config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        return target, []

    try:
        existing = target.read_text(encoding="utf-8")
        with target.open("rb") as handle:
            present = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        # An unreadable or broken config is the user's to fix; guessing at it
        # would only make the mess bigger.
        return target, []

    topped_up, added = _top_up(existing, present)
    if added:
        try:
            target.write_text(topped_up, encoding="utf-8")
        except OSError:
            return target, []
    return target, added


def _top_up(existing: str, present: dict) -> tuple[str, list[str]]:
    """Add the settings a config has never heard of, and change nothing else."""

    lines = existing.splitlines(keepends=True)
    added: list[str] = []
    # Late to early, so each insertion leaves the earlier offsets valid.
    for section, key, block in reversed(_settings(DEFAULT_CONFIG_TEXT)):
        if key in _table(present, section):
            continue
        at = _end_of(lines, section)
        if at is None:
            lines.append(f"\n[{section}]\n")
            at = len(lines)
        lines[at:at] = ["\n", *block]
        added.append(f"{section}.{key}")
    return "".join(lines), sorted(added)


def _table(raw: dict, section: str) -> dict:
    """The table a `[dotted.header]` names, which TOML has parsed as nesting."""

    here = raw
    for part in section.split("."):
        here = here.get(part) if isinstance(here, dict) else None
        if not isinstance(here, dict):
            return {}
    return here


def _settings(text: str) -> list[tuple[str, str, list[str]]]:
    """Every `section, key, its comment block and line` in the shipped config."""

    found, comments, section = [], [], ""
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section, comments = stripped[1:-1], []
        elif stripped.startswith("#"):
            comments.append(line)
        elif "=" in stripped and section:
            found.append((section, stripped.split("=", 1)[0].strip(), [*comments, line]))
            comments = []
        else:
            comments = []
    return found


def _end_of(lines: list[str], section: str) -> int | None:
    """Where `[section]` ends: the next table, or the end of the file.

    A second `[section]` appended at the bottom would be a duplicate table and
    so a parse error, which is why this inserts rather than appends.
    """

    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{section}]":
            start = index
        elif start is not None and stripped.startswith("[") and stripped.endswith("]"):
            while index > start + 1 and not lines[index - 1].strip():
                index -= 1
            return index
    if start is None:
        return None
    end = len(lines)
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def load(path: Path | None = None) -> Config:
    """Read config from disk, falling back to defaults for anything absent.

    With no path given, `~/.dai/config.toml` wins, and the pre-`~/.dai` file is
    read only while there is no new one. A path given explicitly is exactly
    that file, and never falls back to anything.
    """

    target = path
    if target is None:
        target = config_path()
        if not target.exists() and legacy_config_path().exists():
            target = legacy_config_path()
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
        rigor=_rigor(raw.get("critique") or {}),
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
            branch_prefix=str(snap.get("branch_prefix", "dai/")),
            branch_from=str(snap.get("branch_from", "default") or "default"),
            merge=_merge(snap),
            ignore=_strings(snap.get("ignore"), SnapshotConfig().ignore),
        ),
        engines=engines,
        pricing=pricing,
        source=source,
    )


def _rigor(critique: dict) -> str:
    # A level nobody recognises means "standard": a typo should cost the run its
    # harshness, not the run.
    value = str(critique.get("rigor", STANDARD)).strip().lower()
    return value if value in RIGOR else STANDARD


def _merge(snap: dict) -> Merge:
    # A bool is the older spelling of this setting and still an honest one, so
    # a config written before there was a third answer keeps its own: true is
    # "always", false is "never". Anything nobody recognises means "ask" — a
    # typo should cost the run its automation, not a branch of yours.
    value = snap.get("merge", Merge.ASK)
    if isinstance(value, bool):
        return Merge.ALWAYS if value else Merge.NEVER
    text = str(value).strip().lower()
    if spelled := {"true": Merge.ALWAYS, "false": Merge.NEVER}.get(text):
        return spelled
    return Merge(text) if text in tuple(Merge) else Merge.ASK


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
