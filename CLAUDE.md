# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`dai` — a Python CLI/TUI that makes two coding-agent CLIs argue about a task until they agree. A **solver** engine (default `claude`) does the work in the target directory with write access; a **critic** engine (default `codex`) reviews it read-only and pushes back; the solver rebuts each issue (FIXED / PARTIAL / REJECTED, with reasons). Rounds repeat until the referee calls consensus, deadlock, or a round/budget limit. Exit codes: 0 agreed · 1 did not agree · 2 error.

## Commands

Everything runs through uv (Python ≥ 3.11). `make help` lists all targets.

```bash
make install                    # uv sync — install deps (prod + dev)
make test                       # uv run pytest tests/ -v
make test-quick                 # uv run pytest -q
uv run pytest tests/test_orchestrator.py -k deadlock    # one file / pattern
uv run pytest tests/test_protocol.py::test_name          # one test
make doctor                     # check uv / claude / codex are on PATH
uv run dai "some task"          # run from the checkout (or: make run ARGS="'…'")
uv run dai -C ~/proj "task"     # run against another directory
uv build                        # wheel + sdist (make build); make install-cli installs globally
```

- The test suite is hermetic — no real agent CLIs, no network, no tokens spent. **`make smoke` is not**: it runs a real solver/critic argument in a sandbox and spends money. Don't run it casually.
- pytest works straight from the checkout without installing (`tests/conftest.py` prepends `src/` to `sys.path`). `asyncio_mode = "auto"`, so async test functions need no marker.
- No linter or formatter is configured.

## Architecture

Dependency direction: `__main__` / `tui` → `orchestrator` → `protocol`, `consensus`, `engines`, `budget` → `models`. `snapshot` and `transcript` are side-cars driven by the frontends, not by the debate.

**The loop** (`orchestrator.py`, class `Debate`): solve → critique → referee assessment → rebut → critique again. All non-consensus endings (deadlock, out of rounds, out of budget) funnel through the deadlock policy: `critic` means one final enforced turn where the solver applies the critic's open issues without rebuttal; `solver` means the work stands; `ask` defers to the TUI. `Debate` exposes `pause()/resume()/stop()/inject()` — the TUI's control surface — plus `abort_result()` for the TUI's kill path (`q` cancels the debate task outright instead of waiting for a turn boundary, then asks here for the ABORTED ledger to record). Injected user messages are prepended to the next turn's prompt and explicitly declared to outrank both agents.

**The contract** (`protocol.py`): JSON schemas force each side into a machine-checkable position (critic: verdict/issues/conceded/checked; solver: per-issue FIXED/PARTIAL/REJECTED). The prompts encode the anti-failure rules of a two-LLM loop: an APPROVE with an empty `checked` is rejected (rubber stamp), and a rejected issue may only be re-raised with new evidence. Parsing is forgiving about shape but strict about meaning: a missing verdict is never guessed (critic gets one format-retry; a solver report that isn't JSON falls back to raw text, since the work may exist on disk regardless), an unknown severity becomes `major`, an unparseable action becomes `REJECTED` — it must not read as agreement. Prompts also pin an explicit language rule because models drift languages mid-argument otherwise.

**The referee** (`consensus.py`, class `Referee`): stateful across rounds, because both failure modes — instant rubber-stamp agreement and an infinite loop of restated objections — are invisible in a single round. It fingerprints issue claims (ids are unreliable; models renumber) to detect a stalled open-set → deadlock after `no_progress_rounds`, flags approvals without evidence, flags unanswered repeats, and settles early when only `minor` issues remain (`stop_on_minor_only`).

**Engines** (`engines/`): the contract every engine must satisfy is NDJSON event stream + resumable session + JSON-schema-constrained final answer (`engines/base.py` docstring). `Engine` (ABC) owns all subprocess plumbing — spawn (`start_new_session=True`, so the CLI leads its own process group), chunked line reading (no `readline()` size limit), timeout, termination (SIGTERM→SIGKILL to the whole group via `killpg`, on timeout *and* on task cancellation) — and **never raises for agent-side failures**: they come back as `TurnResult.error` so one bad turn degrades the argument instead of killing the run. Subclasses implement `build_argv` + `handle_event` (+ optional `finalize`); register new engines in `ENGINES` in `engines/__init__.py`. One `Session` object per role, held by `Debate`, carries the session id across rounds so each agent keeps its conversation history and prompt cache.

Engine quirks are documented in each adapter's docstring (with observed stream shapes). Key ones:
- `claude.py`: schema passed inline (`--json-schema`), structured output arrives natively, session id is chosen up-front (`--session-id`) so it's addressable even if the first call dies. Access via `--permission-mode`.
- `codex.py`: schema via temp file (`wants_schema_file`); the structured answer arrives as message text and is recovered in `finalize()` via `parse_json_object`; multiple `agent_message` events arrive and **only the last is the answer** (earlier ones are narration); argv order is load-bearing (parent flags like `--sandbox`/`--model` must precede the `resume` subcommand); reports tokens but never cost. Access via `--sandbox`.

**Access control**: `models.Access` — solver gets WRITE (READ_ONLY under `--dry-run`), critic is always READ_ONLY. Enforced through per-engine CLI flags, with role-specific defaults in config (`solve_args` / `critic_args`).

**Money** (`budget.py`): claude reports measured cost; codex only tokens, so its cost is estimated from `[pricing.codex]` config — zero/absent prices mean *unknown, never free*, and the engine is surfaced as "unmeasured" in every report. `room_for_another_turn()` forecast-stops before an unaffordable round (mid-round cutoff leaves a half-modified tree); `resources_exhausted()` deliberately ignores the round limit so a deadlock resolution can still run its one enforced turn.

**Snapshots** (`snapshot.py`): before each round and at the end, every git repo found under the workdir (multi-repo aware, depth-limited scan) is captured via a throwaway `GIT_INDEX_FILE` + `write-tree` + `commit-tree`, parked at `refs/dai/<run-id>/<label>`. HEAD, branch, index, and working tree are never touched — this is a hard invariant of the tool. `.dai/` is hidden via `.git/info/exclude` (not `.gitignore` — it must not appear in the user's diffs or commits).

**Transcripts** (`transcript.py`): append-only `.dai/runs/<run-id>/events.jsonl` written as the run happens, plus `report.md` at the end; `--runs` / `--show` read these. A read-only workspace silently disables recording rather than stopping the run.

**Frontends**: `__main__.py` wires config → engines → `Debate`, then picks headless streaming (non-TTY or `--no-tui`) or the Textual TUI (`tui/app.py`). Both attach the same four `Debate` callbacks (`on_event`, `on_agent_event`, `on_round_start` for snapshots, `on_deadlock`). The TUI runs the debate as a Textual worker on the app's own event loop, so callbacks touch widgets directly.

`tui/theme.py` owns the look: two `Palette`s (`DARK`/`LIGHT`) and the Textual `Theme` built from each, registered and selected in each app's `__init__`, not `on_mount` — the stylesheet is parsed before mount. Nothing in it is a constant: `theme.MUTED` and `theme.S_ERROR` go through a module `__getattr__` and resolve against the *active* palette on every access, so a mid-run theme change reaches everything that reads a colour at render time. Two consequences: nothing outside the module may store a resolved colour (log lines store a style *name* and call `style()` when they draw — that is what `AgentPane`/`VerdictLog`'s `_Line`/`_Entry` recipes are for, since a `RichLog` keeps rendered strips that no Textual API will re-render), and inside the module a bare `MUTED` is a `NameError`, so the helpers call `color()`. It also holds the helpers for things a terminal cannot style (`spaced` for letterspacing, `keycap`/`hint` for key rows, `meter` for the round bar).

`tui/appearance.py` decides which palette that is. `detect()` asks the terminal with OSC 11 before the first frame is drawn (a palette chosen later is a flash of the wrong one), falling back to `COLORFGBG` and then to dark. `AppearanceDriver` — Textual's `LinuxDriver` with our parser lent to its input loop — turns on mode 2031 so the terminal reports scheme changes, and `SchemeParser` takes those reports, and any late OSC 11 answer, out of the input stream: Textual reissues escape sequences it does not recognise as literal keypresses, so anything left in would be typed into whatever has focus. It holds back a cut-off sequence across reads but never a bare `\x1b`, which is the escape key. A terminal that does not support mode 2031 simply never reports, and the palette stays as it was found. `App.watch_theme` (in the `FollowsTerminal` mixin) then repaints, after Textual's own CSS refresh, which it is guaranteed to follow because both are queued with `call_next`. `tui/completion.py` implements the `@` path picker; it inserts a plain relative path — the `@` never reaches the agents (codex has no `@`-syntax and both sides must receive identical text). Its `PromptArea` is a `TextArea` wearing an `Input`'s `value`/`cursor_position` surface, so the mention logic stayed put; `Enter` commits and `shift+enter`/`ctrl+j`/`alt+enter` break the line, all intercepted in `_on_key` because a `TextArea` swallows `enter` and the arrows before bindings run.

## Conventions and gotchas

- **Adding a config option means touching several places in step**: the `Config` dataclass, `from_dict()` fallbacks, and `DEFAULT_CONFIG_TEXT` (the annotated file `--init` writes) in `config.py` — plus `build_parser()` and `_apply_overrides()` in `__main__.py` if it's flag-overridable.
- **Testing approach — three layers, no real CLIs anywhere**: engine adapters are replayed end-to-end with `cat` streaming captured real CLI output (`tests/fixtures/*.jsonl`) through the genuine `Engine.run()` plumbing; the orchestrator/referee are driven by the `Scripted` engine returning canned structured payloads (`test_orchestrator.py`, imported by `test_tui.py`); the TUI is driven headlessly with Textual's `run_test()` pilot. New stream-parsing behaviour should come with a captured fixture.
- **`tui/styles.tcss` declares no colours at all**, `$dai-*` included: they come from the theme's `variables`, which is the only way they can change with it. A `$var:` in the source is *appended* to the theme's value rather than replacing it, which looks like an override for a single colour and silently corrupts anything longer (`$dai-solver 35%`). A bare host that loads the sheet — `PaneHost` in `tests/test_tui.py`, `Harness` in `tests/test_completion.py` — has to call `apply_theme()` first.
- **Nothing may capture a colour at import time.** A module-level `{Severity.MAJOR: theme.S_WARNING}` freezes the palette when the module loads and never follows a theme change; the severity, phase and outcome tables hold *names* and resolve on use. Same for default arguments — `AgentPane` takes a role, not an accent.
- **`RichLog` is constructed with `min_width=1`** in the panes and the verdict log. The stock 78 renders every line 78 columns wide and lets the widget clip it, which in a half-width pane silently eats the tail of every sentence instead of wrapping it.
- Errors degrade, never crash: engine failure → `TurnResult.error`; unwritable transcript → disables itself; per-repo snapshot failure → a "skipped" note. Keep this property when extending.
- `make help` is generated from the `## ` comments on Makefile targets; the README's tables mirror them — keep both in sync when adding targets or flags.
