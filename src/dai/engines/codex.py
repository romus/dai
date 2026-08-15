"""Codex CLI engine.

Stream shape, as observed from the CLI directly:

    {"type":"thread.started","thread_id":"…"}
    {"type":"turn.started"}
    {"type":"item.completed","item":{"type":"agent_message","text":"…"}}     ← preamble
    {"type":"item.completed","item":{"type":"command_execution",
                                     "command":"wc -l data.txt","exit_code":0,…}}
    {"type":"item.completed","item":{"type":"agent_message","text":"…"}}     ← real answer
    {"type":"turn.completed","usage":{…}}

Two traps live in that stream. Codex narrates before it works, so `agent_message`
arrives more than once and only the last one is the answer. And it reports no
cost — only tokens — so money is estimated from config pricing, never measured.
"""

from __future__ import annotations

from pathlib import Path

from dai.engines.base import Engine, Session, _Turn, clip, parse_json_object
from dai.models import Access, AgentEvent, Usage


class CodexEngine(Engine):
    name = "codex"

    #: Codex takes its schema as a path, unlike Claude which takes it inline.
    wants_schema_file = True

    def build_argv(
        self,
        prompt: str,
        *,
        access: Access,
        schema: dict | None,
        schema_path: Path | None,
        session: Session,
    ) -> list[str]:
        # Argument order is load-bearing. `exec resume` is a subcommand with a
        # narrow option set — it rejects --sandbox and --model outright — so
        # everything the parent owns has to come before the subcommand.
        argv = [self.cmd, "exec"]

        if not any(a in ("--sandbox", "-s") for a in self.extra_args):
            argv += [
                "--sandbox",
                "workspace-write" if access is Access.WRITE else "read-only",
            ]
        if self.model:
            argv += ["--model", self.model]
        argv += self.extra_args

        if session.id:
            argv += ["resume", session.id]
        argv += [prompt, "--json", "--skip-git-repo-check"]

        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]

        return argv

    def handle_event(self, event: dict, turn: _Turn) -> None:
        kind = event.get("type")

        if kind == "thread.started":
            turn.result.session_id = event.get("thread_id") or turn.result.session_id
            return

        if kind == "item.completed":
            self._handle_item(event.get("item") or {}, turn)
            return

        if kind == "turn.completed":
            turn.result.usage = _usage(event.get("usage") or {})
            return

        if kind in ("turn.failed", "error"):
            detail = event.get("error") or event.get("message") or event
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            turn.result.error = f"codex failed: {detail}"

    def _handle_item(self, item: dict, turn: _Turn) -> None:
        kind = item.get("type")

        if kind == "agent_message":
            # Last message wins: earlier ones are the agent narrating its plan.
            turn.result.text = item.get("text") or ""
            turn.emit(AgentEvent("text", turn.result.text))

        elif kind == "command_execution":
            code = item.get("exit_code")
            status = "" if code in (0, None) else f"exit {code}"
            turn.emit(AgentEvent("tool", "bash", clip(item.get("command", "")) ))
            if status:
                turn.emit(AgentEvent("tool_result", "bash", status))

        elif kind == "file_change":
            changes = item.get("changes") or []
            paths = ", ".join(str(c.get("path", "")) for c in changes if isinstance(c, dict))
            turn.emit(AgentEvent("tool", "edit", clip(paths or item.get("path", ""))))

        elif kind == "reasoning":
            if text := item.get("text"):
                turn.emit(AgentEvent("thinking", clip(text)))

        elif kind == "error":
            turn.result.error = f"codex: {item.get('message') or item}"

    def finalize(self, turn: _Turn) -> None:
        """Recover the schema-shaped answer, which arrives as message text."""

        if turn.result.structured is None and turn.result.text:
            turn.result.structured = parse_json_object(turn.result.text)


def _usage(raw: dict) -> Usage:
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(raw.get("cached_input_tokens") or 0),
        cache_write_tokens=int(raw.get("cache_write_input_tokens") or 0),
        reasoning_tokens=int(raw.get("reasoning_output_tokens") or 0),
    )
