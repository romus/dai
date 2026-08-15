"""Engine registry."""

from __future__ import annotations

from dai.engines.base import Engine, Session
from dai.engines.claude import ClaudeEngine
from dai.engines.codex import CodexEngine

ENGINES: dict[str, type[Engine]] = {
    "claude": ClaudeEngine,
    "codex": CodexEngine,
}


def build_engine(name: str, **kwargs) -> Engine:
    """Instantiate an engine by name, e.g. ``build_engine("claude", model="opus")``."""

    try:
        cls = ENGINES[name]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise ValueError(f"unknown engine {name!r} (known: {known})") from None
    return cls(**kwargs)


__all__ = ["ENGINES", "Engine", "Session", "ClaudeEngine", "CodexEngine", "build_engine"]
