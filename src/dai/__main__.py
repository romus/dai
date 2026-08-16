"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from dai import __version__, config as config_module
from dai.budget import Budget, Limits
from dai.config import Config, Merge, SnapshotConfig
from dai.consensus import Referee
from dai.engines import Engine, build_engine
from dai.models import AgentEvent, Outcome, Role
from dai.orchestrator import Debate, DebateEvent, DebateResult
from dai.protocol import RIGOR
from dai.snapshot import (
    MergeCandidate,
    Snapshotter,
    describe,
    find_repos,
    ignore_locally,
    list_branches,
    merge_promise,
    toplevel,
)
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
    parser.add_argument(
        "--rigor",
        choices=RIGOR,
        help="how hard the two lean on each other when reviewing",
    )
    parser.add_argument("--lang", help='language the agents argue in; "auto" follows the task')
    parser.add_argument(
        "--theme",
        choices=("auto", "dark", "light"),
        help='TUI palette; "auto" follows the terminal',
    )
    parser.add_argument("--no-tui", action="store_true", help="plain streaming output")
    parser.add_argument(
        "--no-snapshot", action="store_true", help="do not commit each round to a branch"
    )
    parser.add_argument(
        "--branch-from",
        metavar="BRANCH",
        help='what the run branches from and merges back into; "current" or a name',
    )
    # Three switches rather than `--merge {always,ask,never}`: the task is a
    # positional, so an optional-valued --merge would have argparse swallow
    # `dai --merge "fix the parser"` as the flag's value and die on it.
    parser.add_argument(
        "--merge", action="store_true", help="on agreement, merge back without asking"
    )
    parser.add_argument(
        "--ask-merge", action="store_true", help="on agreement, choose what to merge"
    )
    parser.add_argument(
        "--no-merge", action="store_true", help="leave the work on the run's branch"
    )
    parser.add_argument("--config", type=Path, help="use a specific config file")
    parser.add_argument("-C", "--cwd", type=Path, help="work in this directory")
    parser.add_argument("--init", action="store_true", help="write the default config and exit")
    parser.add_argument("--runs", action="store_true", help="list past runs here")
    parser.add_argument(
        "--snapshots", action="store_true", help="list the branches dai committed to"
    )
    parser.add_argument("--show", metavar="RUN_ID", help="print a past run's report")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="watch a canned run — no agents, no tokens, nothing written",
    )
    parser.add_argument("--version", action="version", version=f"dai {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = (args.cwd or Path.cwd()).resolve()

    if args.demo:
        # Before the engines are built, so it runs with neither CLI installed —
        # which is the state of the user most likely to want it.
        from dai import demo

        return demo.run(cwd, appearance=_appearance(args.theme or "auto"))
    if args.init:
        path, added = config_module.ensure_config(args.config)
        print(f"config: {path}")
        for name in added:
            print(f"  added {name}")
        return EXIT_OK
    if args.runs:
        return _list_runs(cwd)
    if args.show:
        return _show_run(cwd, args.show)
    if args.snapshots:
        # Deliberately not through _apply_overrides: --no-snapshot switches off
        # committing, not the ability to look at what was already committed.
        return _list_snapshots(cwd, config_module.load(args.config).snapshot)

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
        # This is the moment a user has nothing to run and has just typed out a
        # task for nothing, so it is the moment to mention the one mode that
        # needs neither CLI.
        print("dai: `dai --demo` shows you a run without them", file=sys.stderr)
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
        rigor=cfg.rigor,
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
        # Printed out here, not inside the app: Textual draws on the alternate
        # screen and wipes it on exit, so anything said in the TUI is gone the
        # moment the user quits. The scrollback is the only durable surface.
        _report(result)
        _where(snapshotter, cwd, app.report_path)

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
    if args.rigor:
        cfg.rigor = args.rigor
    if args.lang:
        cfg.language = args.lang
    if getattr(args, "theme", None):
        cfg.theme = args.theme
    if getattr(args, "branch_from", None):
        cfg.snapshot.branch_from = args.branch_from
    # Order is load-bearing: the more cautious flag wins if both are given.
    if getattr(args, "merge", False):
        cfg.snapshot.merge = Merge.ALWAYS
    if getattr(args, "ask_merge", False):
        cfg.snapshot.merge = Merge.ASK
    if getattr(args, "no_merge", False):
        cfg.snapshot.merge = Merge.NEVER
    # Nothing is written during a dry run, so there is nothing to commit — and
    # so nothing to merge either, whatever the config or `--merge` asked for.
    if getattr(args, "no_snapshot", False) or getattr(args, "dry_run", False):
        cfg.snapshot.enabled = False
        cfg.snapshot.merge = Merge.NEVER

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


def _list_snapshots(cwd: Path, settings: SnapshotConfig) -> int:
    """Every branch dai has left here, grouped by the repository holding it."""

    # Its own guard: this runs before main()'s, so that `--init -C /nowhere`
    # keeps working.
    if not cwd.is_dir():
        print(f"dai: {cwd} is not a directory", file=sys.stderr)
        return EXIT_ERROR

    repos = find_repos(cwd, depth=settings.scan_depth, ignore=settings.ignore)
    if not repos:
        print(f"no git repositories under {cwd}")
        return EXIT_OK

    found = list_branches(cwd, settings)
    if not found:
        # Two different diagnoses, and the difference is the whole point of the
        # command: "there is no git here" is not "the run committed nothing".
        print(f"no dai branches in {len(repos)} repo(s) under {cwd}")
        return EXIT_OK

    # Repositories that hold something first. The empty branches are worth
    # naming — they are what an untouched repo looks like, which is not the
    # same as a missing one — but they must not bury the one you came for.
    empty = []
    for repo in repos:
        here = [info for info in found if info.repo == repo]
        if not here:
            continue
        if not any(info.commits for info in here):
            empty.append(repo.name)
            continue
        print(_relative(str(repo), cwd) if repo != cwd else ".")
        for info in here:
            plural = "" if info.commits == 1 else "s"
            counted = f"{info.commits} commit{plural}"
            print(f"  {info.branch}  {_colour(counted, DIM)}  {info.when}  {info.subject}")

    if empty:
        print(_colour(f"nothing committed in: {', '.join(empty)}", DIM))
    print(_colour("read one: git -C <repo> log --oneline <branch>", DIM))
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
        # Moving the repo onto a branch has to be said before the run, not
        # discovered after it.
        banner = (
            f"any of {len(snapshotter.repos)} repo(s) that changes moves onto "
            f"{snapshotter.branch}, a commit per round"
        ) + merge_promise(snapshotter.settings.merge)
        print(_colour(banner, DIM))
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
        # Committing shells out to git; keep it off the event loop.
        report = await asyncio.to_thread(
            snapshotter.capture_gate, number, debate.last_verdict
        )
        transcript.snapshots(report)
        for note in report.skipped:
            print(f"  {_colour('!', YELLOW)} commit skipped — {note}")

    debate.on_event = on_event
    debate.on_agent_event = on_agent
    debate.on_round_start = on_round_start

    result = await debate.run()

    if snapshotter.active:
        final = await asyncio.to_thread(
            snapshotter.capture_final, f"{result.outcome.value}: {result.reason}"
        )
        transcript.snapshots(final)
        if result.agreed and snapshotter.settings.merge is not Merge.NEVER:
            await _settle_merge(snapshotter, snapshotter.settings.merge, cwd)

    report_path = transcript.finish(
        result,
        task=debate.task,
        cwd=cwd,
        solver=debate.solver.name,
        critic=debate.critic.name,
        repos=snapshotter.summary(),
    )
    _report(result)
    _where(snapshotter, cwd, report_path)
    return result


async def _settle_merge(snapshotter: Snapshotter, merge: Merge, cwd: Path) -> None:
    """Decide what goes home, and move it. Only ever called on agreement.

    `always` is the old behaviour untouched. `ask` shows what each repository
    would write and takes an answer — but only if there is somebody to answer:
    piped or redirected, nothing is merged and the record says why, which is
    the same way `deadlock_policy = "ask"` degrades with nobody to ask.
    """

    if merge is Merge.ALWAYS:
        await asyncio.to_thread(snapshotter.merge)
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        await asyncio.to_thread(
            snapshotter.merge, only=(), kept="no terminal to ask on"
        )
        return

    rows = await asyncio.to_thread(snapshotter.preview)
    if not rows:
        return
    chosen = await asyncio.to_thread(_ask_merge, rows)
    await asyncio.to_thread(
        snapshotter.merge, only=chosen, kept="you kept the branch"
    )


def _ask_merge(rows: list[MergeCandidate]) -> list[Path]:
    """Show what would be written, and ask. All of it or none of it.

    Deliberately not a picker: a selector built out of raw stdin is worse than
    an honest yes or no, and the one that can pick repository by repository is
    the TUI. Anything but yes keeps every branch, which is also what an
    unreadable stdin and an interrupt mean.
    """

    print()
    print(_colour(f"{_repos(len(rows))} changed:", BOLD))
    for row in rows:
        counts = " ".join(
            part
            for part in (
                _colour(f"+{row.added}", GREEN) if row.added else "",
                _colour(f"-{row.removed}", RED) if row.removed else "",
            )
            if part
        )
        mark = " " if row.mergeable else "!"
        print(f"  {mark} {row.label} → {row.base_branch}  {counts}")
        detail = (
            f"{row.refusal} — merge by hand"
            if row.refusal
            else " · ".join(row.files[:4])
            + (f" +{len(row.files) - 4} more" if len(row.files) > 4 else "")
        )
        print(f"    {_colour(detail, YELLOW if row.refusal else DIM)}")

    ready = [row.repo for row in rows if row.mergeable]
    if not ready:
        print(_colour("none of them can be merged — the branches stay", DIM))
        return []

    try:
        answer = input(
            f"merge {_repos(len(ready))} into their base branches? [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    return ready if answer in ("y", "yes") else []


def _repos(number: int) -> str:
    return f"{number} {'repository' if number == 1 else 'repositories'}"


def _where(snapshotter: Snapshotter, cwd: Path, report_path: Path | None) -> None:
    """Where the work ended up — the last thing said, in both frontends."""

    for line in describe(snapshotter.summary(), cwd):
        print(_colour(line, DIM) if line.startswith(" ") else line)
    if report_path is not None:
        print(_colour(f"transcript: {report_path}", DIM))


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
