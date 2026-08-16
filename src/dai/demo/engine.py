"""An engine that argues with itself, instantly and for free."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dai.engines.base import Engine, Session, _Turn
from dai.models import Access, AgentEvent, TurnResult, Usage

#: Pause between the events one turn emits. Nothing in the debate loop sleeps,
#: so an unpaced fake run is over before Textual draws a single frame — and a
#: screen you cannot watch is the one thing this whole module exists to avoid.
BEAT = 0.06


class FakeEngine(Engine):
    """Replays canned answers, with just enough theatre to be watchable.

    Modelled on the `Scripted` double the tests use: `run()` is overridden
    outright, so the subprocess plumbing below it never runs and the two
    abstract methods exist only to satisfy the ABC.

    One class serves both roles. Which one it is playing is decided by the
    schema it was handed rather than by how it was built, because the two
    engines are constructed identically and only the orchestrator knows which
    seat each is in.
    """

    name = "fake"

    def __init__(self, **kwargs) -> None:
        # Not "fake", and not /bin/true either: `_missing_binaries` runs
        # `shutil.which` over both engines before the TUI opens, and macOS has
        # no /bin/true. The interpreter that is running us is executable by
        # construction, on every platform. It is never actually spawned.
        kwargs.pop("cmd", None)
        super().__init__(cmd=sys.executable, **kwargs)
        self.solves: list[dict] = []
        self.critiques: list[dict] = []
        self.beat = BEAT
        self._turn = 0

    def build_argv(  # pragma: no cover - nothing is ever spawned
        self,
        prompt: str,
        *,
        access: Access,
        schema: dict | None,
        schema_path: Path | None,
        session: Session,
    ) -> list[str]:
        return [self.cmd]

    def handle_event(self, event: dict, turn: _Turn) -> None:  # pragma: no cover
        pass

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        access: Access = Access.READ_ONLY,
        schema: dict | None = None,
        session: Session | None = None,
        on_event=None,
        timeout: float | None = None,
    ) -> TurnResult:
        self._turn += 1
        critiquing = self._is_critic_schema(schema)
        script = self.critiques if critiquing else self.solves
        payload = script.pop(0) if script else None

        await self._perform(critiquing, cwd, access, on_event)

        if payload is None:
            return TurnResult(error=f"{self.name}: nothing left in the script")
        if isinstance(payload, str):
            return TurnResult(error=payload)
        return TurnResult(
            structured=payload,
            text="ok",
            cost_usd=self.cost,
            usage=Usage(output_tokens=120, input_tokens=900),
            session_id=session.id if session and session.id else "fake-session",
        )

    #: What one turn claims to have cost. Enough to move the meter; override it
    #: to walk a run into `Outcome.BUDGET`.
    cost = 0.01

    @staticmethod
    def _is_critic_schema(schema: dict | None) -> bool:
        return bool(schema) and "verdict" in (schema.get("properties") or {})

    async def _perform(self, critiquing, cwd, access, on_event) -> None:
        """Say something, slowly enough to be read.

        Nothing here writes, and nothing here reads either — a turn is its
        commentary and nothing else. `AgentPane` buffers streamed text until a
        newline and renders only four kinds of event, so this emits whole lines
        and sticks to what the pane will actually draw.
        """

        for event in _CRITIC_CHATTER if critiquing else _SOLVER_CHATTER:
            if on_event is not None:
                on_event(event)
            if self.beat:
                await asyncio.sleep(self.beat)


def _say(kind: str, text: str, detail: str = "") -> AgentEvent:
    return AgentEvent(kind=kind, text=text, detail=detail)


_SOLVER_CHATTER = [
    _say("thinking", "reading the task and the tree"),
    _say("tool", "Read", "README.md"),
    _say("tool", "Read", "matrix.md"),
    _say("text", "The table has empty cells; the README has the answers.\n"),
    _say("tool", "Edit", "matrix.md"),
    _say("text", "Filled them in and left the formatting alone.\n"),
]

_CRITIC_CHATTER = [
    _say("thinking", "opening what the solver says it changed"),
    _say("tool", "Read", "matrix.md"),
    _say("tool", "Read", "README.md"),
    _say("text", "Checked each cell against the source of truth.\n"),
]
