"""Claude Code engine.

Stream shape, as observed from the CLI directly:

    {"type":"system","subtype":"init","session_id":"…"}
    {"type":"system","subtype":"thinking_tokens"}          ← noise
    {"type":"stream_event","event":{"type":"content_block_delta",
                                    "delta":{"type":"text_delta","text":"…"}}}
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read",…}]}}
    {"type":"user","message":{"content":[{"type":"tool_result",…}]}}
    {"type":"result","subtype":"success","result":"…","structured_output":{…},
     "total_cost_usd":0.02,"usage":{…}}
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from dai.engines.base import Engine, Session, _Turn, clip
from dai.models import Access, AgentEvent, Usage

# Which argument best identifies what a tool call is doing, for the UI pane.
_TOOL_SUBJECT = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "notebook_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "Task": "description",
}


class ClaudeEngine(Engine):
    name = "claude"

    def build_argv(
        self,
        prompt: str,
        *,
        access: Access,
        schema: dict | None,
        schema_path: Path | None,
        session: Session,
    ) -> list[str]:
        argv = [
            self.cmd,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]

        if session.id:
            argv += ["--resume", session.id]
        else:
            # Claude lets us choose the id up front, so the session is
            # addressable even if the first call dies before reporting it.
            session.id = str(uuid.uuid4())
            argv += ["--session-id", session.id]

        if self.model:
            argv += ["--model", self.model]

        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]

        # A default only: config supplies the real permission flags per role.
        if not any(a == "--permission-mode" for a in self.extra_args):
            argv += [
                "--permission-mode",
                "acceptEdits" if access is Access.WRITE else "plan",
            ]

        return argv + self.extra_args

    def handle_event(self, event: dict, turn: _Turn) -> None:
        kind = event.get("type")

        if kind == "system":
            if event.get("subtype") == "init":
                turn.result.session_id = event.get("session_id") or turn.result.session_id
            return

        if kind == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    turn.emit(AgentEvent("text", delta["text"]))
            return

        if kind == "assistant":
            for block in _blocks(event):
                if block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    args = block.get("input") or {}
                    key = _TOOL_SUBJECT.get(name)
                    subject = args.get(key) if key else ""
                    if not subject and isinstance(args, dict):
                        subject = next((str(v) for v in args.values() if v), "")
                    turn.emit(AgentEvent("tool", name, clip(subject)))
            return

        if kind == "result":
            turn.result.text = event.get("result") or ""
            structured = event.get("structured_output")
            if isinstance(structured, dict):
                turn.result.structured = structured
            turn.result.cost_usd = event.get("total_cost_usd")
            turn.result.usage = _usage(event.get("usage") or {})
            turn.result.session_id = event.get("session_id") or turn.result.session_id
            if event.get("is_error") or event.get("subtype") != "success":
                turn.result.error = (
                    event.get("error")
                    or event.get("api_error_status")
                    or f"claude finished as {event.get('subtype')!r}"
                )


def _blocks(event: dict) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _usage(raw: dict) -> Usage:
    details = raw.get("output_tokens_details") or {}
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        reasoning_tokens=int(details.get("thinking_tokens") or 0),
    )
