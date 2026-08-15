"""Engine abstraction: run a coding CLI headlessly and parse its event stream.

Both supported CLIs emit newline-delimited JSON, keep a resumable session, and
accept a JSON Schema that forces their final answer into a fixed shape. Those
three properties are what let dai detect agreement mechanically instead of
guessing from prose, so they are the contract every engine must satisfy.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

from dai.models import Access, AgentEvent, TurnResult, Usage

EventSink = Callable[[AgentEvent], None] | None

# Read in fixed-size chunks and split lines ourselves. asyncio's readline() caps
# a line at its stream limit and raises once a tool result exceeds it, which is
# routine when an agent cats a large file.
_CHUNK = 64 * 1024


@dataclass
class Session:
    """Conversation continuity for one agent across rounds.

    Engines mutate `id` on first use, so passing the same Session into
    successive calls resumes the conversation — preserving the argument's
    history and reusing the provider's prompt cache.
    """

    id: str | None = None


@dataclass
class _Turn:
    """Mutable scratch state while an engine's event stream is consumed."""

    result: TurnResult = field(default_factory=TurnResult)
    sink: EventSink = None

    def emit(self, event: AgentEvent) -> None:
        if self.sink is not None:
            self.sink(event)


class Engine(ABC):
    """A coding CLI that dai can drive headlessly."""

    name: str = "engine"

    def __init__(
        self,
        *,
        cmd: str | None = None,
        model: str = "",
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.cmd = cmd or self.name
        self.model = model
        self.extra_args = list(extra_args or [])
        self.env = env

    # --- subclass contract -------------------------------------------------

    @abstractmethod
    def build_argv(
        self,
        prompt: str,
        *,
        access: Access,
        schema: dict | None,
        schema_path: Path | None,
        session: Session,
    ) -> list[str]:
        """Assemble the command line. May set `session.id` for a fresh session."""

    @abstractmethod
    def handle_event(self, event: dict, turn: _Turn) -> None:
        """Fold one parsed JSON event into the turn being built."""

    def finalize(self, turn: _Turn) -> None:
        """Hook for engines needing post-processing once the stream ends."""

    #: Set when the engine wants its schema written to a file rather than
    #: passed inline on the command line.
    wants_schema_file: bool = False

    # --- shared runner -----------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        access: Access = Access.READ_ONLY,
        schema: dict | None = None,
        session: Session | None = None,
        on_event: EventSink = None,
        timeout: float | None = None,
    ) -> TurnResult:
        """Invoke the CLI once and return its parsed outcome.

        Never raises for an agent-side failure: a non-zero exit, a missing
        binary or a malformed stream all come back as `TurnResult.error`, so a
        single bad round degrades the argument instead of killing the run.
        """

        session = session or Session()
        turn = _Turn(sink=on_event)
        started = time.monotonic()
        schema_path: Path | None = None
        tmpdir: tempfile.TemporaryDirectory | None = None

        try:
            if schema is not None and self.wants_schema_file:
                tmpdir = tempfile.TemporaryDirectory(prefix="dai-schema-")
                schema_path = Path(tmpdir.name) / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")

            argv = self.build_argv(
                prompt,
                access=access,
                schema=schema,
                schema_path=schema_path,
                session=session,
            )
            turn.result.argv = argv

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    # codex reads stdin when it is a pipe and would block forever.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, **(self.env or {})},
                    # The CLI leads its own process group, so termination can
                    # reach its children too (MCP servers, shell tools). In
                    # dai's own group, killpg would take dai down with it.
                    start_new_session=True,
                )
            except FileNotFoundError:
                turn.result.error = f"{self.cmd!r} not found in PATH"
                return turn.result
            except OSError as exc:
                turn.result.error = f"failed to start {self.cmd!r}: {exc}"
                return turn.result

            try:
                await self._pump(proc, turn, timeout)
            except asyncio.CancelledError:
                await _terminate(proc)
                raise

            if proc.returncode not in (0, None) and turn.result.error is None:
                stderr = (await proc.stderr.read()).decode("utf-8", "replace")
                turn.result.error = (
                    f"{self.name} exited with {proc.returncode}"
                    + (": " + cause if (cause := _explain(stderr)) else "")
                )

            self.finalize(turn)
            if session.id is None:
                session.id = turn.result.session_id
            turn.result.session_id = turn.result.session_id or session.id
            return turn.result
        finally:
            turn.result.duration_s = time.monotonic() - started
            if tmpdir is not None:
                tmpdir.cleanup()

    async def _pump(
        self, proc: asyncio.subprocess.Process, turn: _Turn, timeout: float | None
    ) -> None:
        """Consume the event stream, honouring an optional wall-clock budget."""

        async def consume() -> None:
            assert proc.stdout is not None
            async for line in _iter_lines(proc.stdout):
                if not line.startswith("{"):
                    continue  # progress chatter, e.g. codex's stdin notice
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.handle_event(event, turn)
            await proc.wait()

        try:
            await asyncio.wait_for(consume(), timeout=timeout)
        except TimeoutError:
            await _terminate(proc)
            turn.result.error = f"{self.name} exceeded its {timeout:.0f}s time budget"


async def _iter_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield decoded lines without asyncio's per-line size ceiling."""

    buf = b""
    while True:
        chunk = await stream.read(_CHUNK)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace").strip()
    if buf:
        yield buf.decode("utf-8", "replace").strip()


# Boilerplate a CLI prints after the real error; keeping it buries the cause.
_NOISE = ("usage:", "for more information", "tip:", "note:")


def _explain(stderr: str) -> str:
    """Pull the actual reason out of a CLI's stderr.

    The cause comes first and the usage block follows, so reporting the tail —
    the intuitive choice — reliably shows everything except what went wrong.
    """

    lines = [
        line.strip()
        for line in stderr.splitlines()
        if line.strip() and not line.strip().lower().startswith(_NOISE)
    ]
    return " / ".join(lines[:3])


def _signal_group(proc: asyncio.subprocess.Process, *, kill: bool = False) -> None:
    """Signal the child's whole process group, falling back to the child alone.

    The group exists because the child is spawned with start_new_session=True;
    signalling only the CLI would orphan its own children.
    """

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL if kill else signal.SIGTERM)
        return
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        pass  # no killpg on this platform, group already gone, or foreign pgid
    try:
        proc.kill() if kill else proc.terminate()
    except ProcessLookupError:
        pass  # already dead


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Stop a child politely, then decisively — even while being cancelled again."""

    if proc.returncode is not None:
        return
    _signal_group(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except TimeoutError:
        _signal_group(proc, kill=True)
        await proc.wait()
    except asyncio.CancelledError:
        # A second cancellation (app teardown, another Ctrl+C) lands here,
        # mid-grace. Skipping the rest of the wait is fine; skipping the
        # kill is not.
        _signal_group(proc, kill=True)
        raise


def clip(text: str, limit: int = 300) -> str:
    """Flatten a tool argument, guarding only against absurd lengths.

    Presentation-level shortening belongs to whoever is drawing the screen: it
    knows the working directory and the pane width, so it can drop a long path
    prefix instead of truncating the filename that matters.
    """

    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def parse_json_object(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from a model's final message.

    Engines that return the schema-shaped answer as text sometimes wrap it in a
    fenced block or add a sentence around it, so a plain `json.loads` is not
    enough on its own.
    """

    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        body = text.split("\n", 1)[1] if "\n" in text else ""
        text = body.rsplit("```", 1)[0].strip() if "```" in body else body.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None
