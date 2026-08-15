"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from dai import __version__, config as config_module
from dai.budget import Budget, Limits
from dai.config import Config
from dai.consensus import Referee
from dai.engines import Engine, build_engine
from dai.models import AgentEvent, Outcome, Role
from dai.orchestrator import Debate, DebateEvent, DebateResult
from dai.snapshot import Snapshotter, ignore_locally, toplevel
from dai.transcript import Transcript, list_runs, new_run_id

EXIT_OK = 0
EXIT_DISAGREED = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dai",
        description="Two CLI agents argue about your task until they agree.",
    )
    parser.add_argument("task", nargs="?", help="what you want done")
    parser.add_argument("-f", "--file", type=Path, help="read the task from a file")
    parser.add_argument("--solver", help="engine that does the work")
    parser.add_argument("--critic", help="engine that reviews it")
    parser.add_argument("--rounds", type=int, help="maximum rounds of criticism")
    parser.add_argument("--budget", type=float, metavar="USD", help="spending limit")
    parser.add_argument(
        "--policy",
        choices=("critic", "solver", "ask"),
        help="who prevails if they will not converge",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="both agents read-only; nothing on disk is modified",
    )
    parser.add_argument("--lang", help='language the agents argue in; "auto" follows the task')
    parser.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        help='TUI palette; "auto" follows the terminal',
    )
    parser.add_argument("--no-tui", action="store_true", help="plain streaming output")
    parser.add_argument("--no-snapshot", action="store_true", help="skip git snapshots")
    parser.add_argument("--config", type=Path, help="use a specific config file")
    parser.add_argument("-C", "--cwd", type=Path, help="work in this directory")
    parser.add_argument("--init", action="store_true", help="write the default config and exit")
    parser.add_argument("--runs", action="store_true", help="list past runs here")
    parser.add_argument("--show", metavar="RUN_ID", help="print a past run's report")
    parser.add_argument("--version", action="version", version=f"dai {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = (args.cwd or Path.cwd()).resolve()

    if args.init:
        print(f"config: {config_module.ensure_config(args.config)}")
        return EXIT_OK
    if args.runs:
        return _list_runs(cwd)
    if args.show:
        return _show_run(cwd, args.show)

    if not cwd.is_dir():
        print(f"dai: {cwd} is not a directory", file=sys.stderr)
        return EXIT_ERROR

    # Loaded before the prompt: the task input reads its own settings from here.
    cfg = _apply_overrides(config_module.load(args.config), args)

    interactive = not args.no_tui and sys.stdout.isatty()
    # Asked once, before anything is drawn: a palette chosen after the first
    # frame is a flash of the wrong one. From here on the TUI keeps up with the
    # terminal by itself.
    appearance = _appearance(cfg.theme) if interactive else "dark"

    task = _resolve_task(args)
    if not task and interactive:
        from dai.tui import ask_for_task

        task = ask_for_task(
            cwd, debounce_ms=cfg.completion_debounce_ms, appearance=appearance
        )
    if not task:
        print('dai: give me a task, e.g. dai "fill in the table in docs/matrix.md"',
              file=sys.stderr)
        return EXIT_ERROR

    try:
        solver, critic = _build_engines(cfg, writing=not args.dry_run)
    except ValueError as exc:
        print(f"dai: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if missing := _missing_binaries(solver, critic):
        print(f"dai: not found on PATH: {', '.join(missing)}", file=sys.stderr)
        return EXIT_ERROR

    run_id = new_run_id()
    # Hide our own bookkeeping before anything writes it, so the first snapshot
    # does not sweep it up and `git status` stays about the user's work.
    if (repo := toplevel(cwd)) is not None:
        ignore_locally(repo, ".dai/")
    transcript = Transcript(cwd, run_id)
    snapshotter = Snapshotter(cwd, run_id, cfg.snapshot)

    debate = Debate(
        task=task,
        cwd=cwd,
        solver=solver,
        critic=critic,
        budget=Budget(cfg.limits, cfg.pricing),
        referee=Referee(
            no_progress_rounds=cfg.no_progress_rounds,
            stop_on_minor_only=cfg.stop_on_minor_only,
        ),
        deadlock_policy=cfg.deadlock_policy,
        language=cfg.language,
        solver_writes=not args.dry_run,
        turn_timeout=cfg.turn_timeout,
    )

    # A pipe or a redirect means nobody is watching a screen; stream instead.
    if not interactive:
        result = asyncio.run(_run_headless(debate, cwd, transcript, snapshotter))
    else:
        from dai.tui import DaiApp

        app = DaiApp(
            debate, cwd=cwd, transcript=transcript, snapshotter=snapshotter,
            debounce_ms=cfg.completion_debounce_ms, appearance=appearance,
        )
        app.run()
        result = app.result
        if result is None:
            print("dai: the run ended before reaching a verdict", file=sys.stderr)
            return EXIT_ERROR
        _report(result)

    return EXIT_OK if result.agreed else EXIT_DISAGREED


# --- wiring ---------------------------------------------------------------


def _appearance(setting: str) -> str:
    """Ask the terminal which way round it is, if we are meant to.

    Imported here rather than at the top: `dai.tui` pulls in Textual, and the
    headless path has no use for it.
    """

    from dai.tui.appearance import detect

    return detect(setting)


def _resolve_task(args) -> str:
    if args.file:
        try:
            return args.file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"dai: cannot read {args.file}: {exc}", file=sys.stderr)
            return ""
    return (args.task or "").strip()


def _apply_overrides(cfg: Config, args) -> Config:
    if args.solver:
        cfg.solver = args.solver
    if args.critic:
        cfg.critic = args.critic
    if args.policy:
        cfg.deadlock_policy = args.policy
    if args.lang:
        cfg.language = args.lang
    if getattr(args, "theme", None):
        cfg.theme = args.theme
    # Nothing is written during a dry run, so there is nothing to snapshot.
    if getattr(args, "no_snapshot", False) or getattr(args, "dry_run", False):
        cfg.snapshot.enabled = False

    limits = cfg.limits
    cfg.limits = Limits(
        max_rounds=args.rounds if args.rounds else limits.max_rounds,
        max_usd=args.budget if args.budget else limits.max_usd,
        max_tokens=limits.max_tokens,
        max_wall_seconds=limits.max_wall_seconds,
    )
    return cfg


def _build_engines(cfg: Config, *, writing: bool) -> tuple[Engine, Engine]:
    solver_cfg = cfg.engine(cfg.solver)
    critic_cfg = cfg.engine(cfg.critic)
    solver = build_engine(
        cfg.solver,
        cmd=solver_cfg.cmd or None,
        model=solver_cfg.model,
        extra_args=solver_cfg.args_for(writing=writing),
    )
    critic = build_engine(
        cfg.critic,
        cmd=critic_cfg.cmd or None,
        model=critic_cfg.model,
        extra_args=critic_cfg.args_for(writing=False),
    )
    return solver, critic


def _missing_binaries(*engines: Engine) -> list[str]:
    return sorted({e.cmd for e in engines if shutil.which(e.cmd) is None})


# --- past runs ------------------------------------------------------------


def _list_runs(cwd: Path) -> int:
    runs = list_runs(cwd)
    if not runs:
        print("no runs recorded here")
        return EXIT_OK
    for info in runs:
        outcome = info.outcome or "unfinished"
        task = info.task if len(info.task) <= 60 else info.task[:59] + "…"
        print(f"{info.run_id}  {outcome:<10}  {task}")
    return EXIT_OK


def _show_run(cwd: Path, run_id: str) -> int:
    report = cwd / ".dai" / "runs" / run_id / "report.md"
    if not report.is_file():
        print(f"dai: no report for run {run_id}", file=sys.stderr)
        return EXIT_ERROR
    print(report.read_text(encoding="utf-8"))
    return EXIT_OK


# --- headless output ------------------------------------------------------

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, YELLOW, RED, BLUE = "\033[32m", "\033[33m", "\033[31m", "\033[34m"

_SEVERITY_COLOUR = {"blocker": RED, "major": YELLOW, "minor": DIM}


def _colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _relative(text: str, cwd: Path, width: int = 100) -> str:
    """Drop the working-directory prefix so the interesting part survives.

    Agents pass absolute paths, which are mostly a prefix the user already
    knows; truncating from the right would eat the filename instead.
    """

    shown = str(text).replace(f"{cwd}/", "").replace(str(cwd), ".")
    return shown if len(shown) <= width else shown[: width - 1] + "…"


async def _run_headless(
    debate: Debate, cwd: Path, transcript: Transcript, snapshotter: Snapshotter
) -> DebateResult:
    print(
        f"{_colour('dai', BOLD)} · {debate.solver.name} solves, "
        f"{debate.critic.name} critiques · {cwd}"
    )
    print(_colour(f"task: {debate.task}", DIM))
    if snapshotter.active:
        print(
            _colour(
                f"snapshotting {len(snapshotter.repos)} repo(s) to refs/dai/{transcript.run_id}/",
                DIM,
            )
        )
    print()

    transcript.start(
        task=debate.task, cwd=cwd, solver=debate.solver.name, critic=debate.critic.name
    )

    def on_event(event: DebateEvent) -> None:
        transcript.event(
            event.kind,
            round=event.round,
            role=event.role.value if event.role else None,
            engine=event.engine,
            text=event.text,
        )
        if event.kind == "turn_start":
            label = {
                Role.SOLVE: "solving",
                Role.CRITIQUE: "reviewing",
                Role.REBUT: "answering",
            }.get(event.role, str(event.role))
            print(
                f"{_colour('▸', BLUE)} round {event.round} · "
                f"{_colour(event.engine, BOLD)} {label}"
            )
        elif event.kind == "note":
            print(f"  {_colour('!', YELLOW)} {event.text}")
        elif event.kind == "finished":
            print()

    def on_agent(role: Role, event: AgentEvent) -> None:
        if event.kind == "tool":
            shown = _relative(event.detail, cwd)
            detail = f" {_colour(shown, DIM)}" if shown else ""
            print(f"    {_colour('·', DIM)} {event.text}{detail}")

    async def on_round_start(number: int) -> None:
        if not snapshotter.active:
            return
        # Snapshotting shells out to git; keep it off the event loop.
        report = await asyncio.to_thread(snapshotter.capture, f"r{number}")
        transcript.snapshots(f"r{number}", report)
        for note in report.skipped:
            print(f"  {_colour('!', YELLOW)} snapshot skipped — {note}")

    debate.on_event = on_event
    debate.on_agent_event = on_agent
    debate.on_round_start = on_round_start

    result = await debate.run()

    if snapshotter.active:
        final = await asyncio.to_thread(snapshotter.capture, "final")
        transcript.snapshots("final", final)

    report_path = transcript.finish(
        result,
        task=debate.task,
        cwd=cwd,
        solver=debate.solver.name,
        critic=debate.critic.name,
    )
    _report(result)
    if report_path is not None:
        print(_colour(f"transcript: {report_path}", DIM))
    return result


def _report(result: DebateResult) -> None:
    for rnd in result.rounds:
        if rnd.critic is None:
            continue
        verdict = rnd.critic.verdict.value
        colour = GREEN if verdict == "APPROVE" else YELLOW
        open_count = len(rnd.critic.open_issues)
        print(
            f"{_colour('◆', colour)} round {rnd.number}: {_colour(verdict, colour)}"
            + (f" · {open_count} open" if open_count else "")
        )
        for issue in rnd.critic.open_issues:
            sev = _colour(issue.severity.value, _SEVERITY_COLOUR[issue.severity.value])
            print(f"    [{issue.id}] ({sev}) {issue.claim}")
            if issue.evidence:
                print(f"          {_colour(issue.evidence, DIM)}")
        if rnd.critic.conceded:
            print(f"    {_colour('conceded: ' + ', '.join(rnd.critic.conceded), DIM)}")

    print()
    banner = {
        Outcome.CONSENSUS: (GREEN, "agreed"),
        Outcome.DEADLOCK: (YELLOW, "deadlocked"),
        Outcome.BUDGET: (YELLOW, "out of budget"),
        Outcome.ROUNDS: (YELLOW, "out of rounds"),
        Outcome.ABORTED: (DIM, "stopped"),
        Outcome.FAILED: (RED, "failed"),
    }[result.outcome]
    print(f"{_colour(banner[1].upper(), banner[0])} — {result.reason}")

    spend = result.spend
    money = f"${spend.usd:.2f}"
    if not spend.exact:
        money += f" (+ unmeasured spend by {', '.join(sorted(spend.unpriced))})"
    print(_colour(f"{spend.turns} turns · {spend.tokens:,} tokens · {money}", DIM))

    if result.open_issues:
        print()
        print("unresolved:")
        for issue in result.open_issues:
            print(f"  [{issue.id}] ({issue.severity.value}) {issue.claim}")


if __name__ == "__main__":
    sys.exit(main())
