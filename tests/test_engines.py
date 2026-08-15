"""Engine parsing, exercised end-to-end against real captured CLI output.

Each replay runs the genuine `Engine.run()` path — subprocess spawn, chunked
line splitting, event dispatch, finalize — with `cat` standing in for the CLI.
That way the tests cover the plumbing, not just the event handlers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import FIXTURES
from dai.engines import ClaudeEngine, CodexEngine, Session, build_engine
from dai.engines.base import parse_json_object
from dai.models import Access, AgentEvent, TurnResult


def replay(engine_cls, fixture: str, **kwargs):
    """An engine whose 'CLI' is `cat` replaying a captured session."""

    path = FIXTURES / fixture

    class Replay(engine_cls):
        def build_argv(self, prompt, **_):
            return ["cat", str(path)]

    return Replay(**kwargs)


async def run_fixture(engine_cls, fixture: str, tmp_path: Path, **kwargs):
    events: list[AgentEvent] = []
    engine = replay(engine_cls, fixture, **kwargs)
    result = await engine.run(
        "irrelevant", cwd=tmp_path, on_event=events.append, session=Session()
    )
    return result, events


# --- claude ---------------------------------------------------------------


async def test_claude_parses_session_text_cost_and_usage(tmp_path):
    result, _ = await run_fixture(ClaudeEngine, "claude_tool_call.jsonl", tmp_path)

    assert result.ok
    assert result.session_id == "cd27bb7c-30d9-4a6b-ae92-a5140d1b68fa"
    assert result.text == "The file has 3 lines."
    assert result.cost_usd == pytest.approx(0.0271819)
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 57
    assert result.usage.cache_read_tokens == 15928
    assert result.usage.cache_write_tokens == 10257
    assert result.usage.reasoning_tokens == 41


async def test_claude_reports_tool_calls_with_their_subject(tmp_path):
    _, events = await run_fixture(ClaudeEngine, "claude_tool_call.jsonl", tmp_path)

    tools = [(e.text, e.detail) for e in events if e.kind == "tool"]
    assert tools == [
        ("Read", "/tmp/probe/data.txt"),
        ("Bash", "wc -l /tmp/probe/data.txt"),
    ]


async def test_claude_streams_text_deltas(tmp_path):
    _, events = await run_fixture(ClaudeEngine, "claude_tool_call.jsonl", tmp_path)

    assert "".join(e.text for e in events if e.kind == "text") == "Let me check."


async def test_claude_prefers_structured_output_over_prose(tmp_path):
    """The verdict must come from `structured_output`, never from `result` text."""

    result, _ = await run_fixture(ClaudeEngine, "claude_structured.jsonl", tmp_path)

    assert result.structured is not None
    assert result.structured["verdict"] == "REQUEST_CHANGES"
    assert result.structured["issues"][0]["id"] == "i1"
    assert result.text == "prose that must NOT be parsed as the verdict"


def test_claude_starts_a_session_then_resumes_it():
    engine = ClaudeEngine()
    session = Session()

    first = engine.build_argv(
        "task", access=Access.WRITE, schema=None, schema_path=None, session=session
    )
    assert "--session-id" in first
    assert session.id, "engine must pin the session id up front"
    assert first[first.index("--permission-mode") + 1] == "acceptEdits"

    second = engine.build_argv(
        "more", access=Access.READ_ONLY, schema=None, schema_path=None, session=session
    )
    assert second[second.index("--resume") + 1] == session.id
    assert "--session-id" not in second
    assert second[second.index("--permission-mode") + 1] == "plan"


def test_claude_passes_schema_inline():
    argv = ClaudeEngine().build_argv(
        "t",
        access=Access.READ_ONLY,
        schema={"type": "object"},
        schema_path=None,
        session=Session(),
    )
    assert argv[argv.index("--json-schema") + 1] == '{"type": "object"}'


# --- codex ----------------------------------------------------------------


async def test_codex_takes_the_last_agent_message_not_the_preamble(tmp_path):
    """Codex narrates before it works; the first message is not the answer."""

    result, _ = await run_fixture(CodexEngine, "codex_tool_call.jsonl", tmp_path)

    assert result.ok
    assert result.text == "The file has 3 lines."
    assert result.session_id == "01a00010-d0e5-7452-90ce-d44384d0c8fa"


async def test_codex_survives_non_json_chatter(tmp_path):
    """The 'Reading additional input from stdin...' line must not derail parsing."""

    result, _ = await run_fixture(CodexEngine, "codex_tool_call.jsonl", tmp_path)

    assert result.ok
    assert result.usage.input_tokens == 20297
    assert result.usage.cache_read_tokens == 11008
    assert result.usage.reasoning_tokens == 52


async def test_codex_reports_commands_it_ran(tmp_path):
    _, events = await run_fixture(CodexEngine, "codex_tool_call.jsonl", tmp_path)

    commands = [e.detail for e in events if e.kind == "tool"]
    assert commands == ["cat data.txt", "wc -l data.txt"]


async def test_codex_recovers_the_verdict_from_message_text(tmp_path):
    """Codex returns schema-shaped answers as text, so finalize must parse it."""

    result, _ = await run_fixture(CodexEngine, "codex_structured.jsonl", tmp_path)

    assert result.structured is not None
    assert result.structured["verdict"] == "REQUEST_CHANGES"
    assert result.structured["checked"] == ["cat docs/matrix.md"]


async def test_codex_reports_no_cost(tmp_path):
    """Codex bills invisibly; budget code must not mistake absence for zero."""

    result, _ = await run_fixture(CodexEngine, "codex_tool_call.jsonl", tmp_path)

    assert result.cost_usd is None


def test_codex_starts_a_thread_then_resumes_it():
    engine = CodexEngine()
    session = Session()

    first = engine.build_argv(
        "task", access=Access.WRITE, schema=None, schema_path=None, session=session
    )
    assert first[:2] == ["codex", "exec"]
    assert "task" in first
    assert first[first.index("--sandbox") + 1] == "workspace-write"

    session.id = "thread-42"
    second = engine.build_argv(
        "more", access=Access.READ_ONLY, schema=None, schema_path=None, session=session
    )
    resume_at = second.index("resume")
    assert second[resume_at + 1 : resume_at + 3] == ["thread-42", "more"]
    assert second[second.index("--sandbox") + 1] == "read-only"


def test_codex_puts_parent_options_before_the_resume_subcommand():
    """Regression: `codex exec resume` rejects --sandbox/--model outright.

    Those belong to the parent `exec` command. Placing them after `resume`
    makes codex exit 2 with 'unexpected argument', which killed every round
    after the first.
    """

    argv = CodexEngine(model="gpt-5").build_argv(
        "more",
        access=Access.READ_ONLY,
        schema=None,
        schema_path=None,
        session=Session(id="thread-42"),
    )

    resume_at = argv.index("resume")
    assert argv.index("--sandbox") < resume_at
    assert argv.index("--model") < resume_at
    # --json and --output-schema are accepted by the subcommand itself.
    assert argv.index("--json") > resume_at


def test_codex_extra_args_land_before_the_subcommand():
    """Config-supplied flags are parent-level too, so they must precede resume."""

    argv = CodexEngine(extra_args=["-c", "model_reasoning_effort=high"]).build_argv(
        "more",
        access=Access.READ_ONLY,
        schema=None,
        schema_path=None,
        session=Session(id="t1"),
    )

    assert argv.index("-c") < argv.index("resume")


def test_codex_passes_schema_as_a_file():
    argv = CodexEngine().build_argv(
        "t",
        access=Access.READ_ONLY,
        schema={"type": "object"},
        schema_path=Path("/tmp/s.json"),
        session=Session(),
    )
    assert argv[argv.index("--output-schema") + 1] == "/tmp/s.json"
    assert CodexEngine.wants_schema_file is True


# --- runner behaviour -----------------------------------------------------


async def test_missing_binary_is_reported_not_raised(tmp_path):
    """One broken engine must degrade the argument, not crash the run."""

    engine = ClaudeEngine(cmd="definitely-not-a-real-binary-xyz")
    result = await engine.run("t", cwd=tmp_path)

    assert not result.ok
    assert "not found in PATH" in result.error


async def test_nonzero_exit_becomes_an_error(tmp_path):
    class Failing(ClaudeEngine):
        def build_argv(self, prompt, **_):
            return ["sh", "-c", "echo boom >&2; exit 3"]

    result = await Failing().run("t", cwd=tmp_path)

    assert not result.ok
    assert "exited with 3" in result.error
    assert "boom" in result.error


async def test_error_message_keeps_the_cause_not_the_usage_text(tmp_path):
    """CLIs print the reason first, then usage. Reporting the tail hides it."""

    class Failing(ClaudeEngine):
        def build_argv(self, prompt, **_):
            return [
                "sh",
                "-c",
                "echo \"error: unexpected argument '--sandbox'\" >&2;"
                " echo >&2; echo 'Usage: codex exec resume ...' >&2;"
                " echo \"For more information, try '--help'.\" >&2; exit 2",
            ]

    result = await Failing().run("t", cwd=tmp_path)

    assert "unexpected argument" in result.error
    assert "For more information" not in result.error


async def test_lines_larger_than_the_read_chunk_survive(tmp_path):
    """A tool result can dwarf asyncio's default line limit; we must not drop it."""

    payload = "x" * 200_000

    class Big(ClaudeEngine):
        def build_argv(self, prompt, **_):
            import json

            event = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": payload,
                    "session_id": "s1",
                    "usage": {},
                }
            )
            return ["printf", "%s\n", event]

    result = await Big().run("t", cwd=tmp_path)

    assert result.ok
    assert result.text == payload


async def test_timeout_terminates_the_child(tmp_path):
    class Hanging(ClaudeEngine):
        def build_argv(self, prompt, **_):
            return ["sleep", "30"]

    result = await Hanging().run("t", cwd=tmp_path, timeout=0.3)

    assert not result.ok
    assert "time budget" in result.error


async def test_cancellation_terminates_and_reaps_the_child(tmp_path, monkeypatch):
    """The TUI's kill switch cancels the running turn; the child must not survive."""

    class Sleeper(ClaudeEngine):
        def build_argv(self, prompt, **_):
            return ["sleep", "30"]

    spawned = []
    real = asyncio.create_subprocess_exec

    async def capture(*argv, **kwargs):
        proc = await real(*argv, **kwargs)
        spawned.append((proc, kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    task = asyncio.create_task(Sleeper().run("t", cwd=tmp_path))
    while not spawned:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    proc, kwargs = spawned[0]
    assert kwargs.get("start_new_session") is True, "the child must lead its own group"
    assert proc.returncode is not None, "the child must be terminated and reaped"


# --- helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Here you go:\n{"a": 1}', {"a": 1}),
        ("not json at all", None),
        ("", None),
        ("[1,2,3]", None),
    ],
)
def test_parse_json_object(raw, expected):
    assert parse_json_object(raw) == expected


def test_build_engine_rejects_unknown_names():
    assert isinstance(build_engine("claude", model="opus"), ClaudeEngine)
    with pytest.raises(ValueError, match="unknown engine"):
        build_engine("gpt-9")
