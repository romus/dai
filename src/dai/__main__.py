"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from dai import __version__, cleanup, config as config_module
from dai.budget import Budget, Limits
from dai.config import Config, Merge, SnapshotConfig
from dai.consensus import Referee
from dai.engines import Engine, build_engine
from dai.models import AgentEvent, Outcome, Role
from dai.orchestrator import (
    Debate,
    DebateEvent,
    DebateResult,
    Objection,
    ObjectionRecap,
)
from dai.protocol import RIGOR
from dai.snapshot import (
    MergeCandidate,
    Snapshotter,
    describe,
    find_repos,
    list_branches,
    merge_promise,
)
from dai.transcript import (
    Transcript,
    find_run,
    list_all_runs,
    list_runs,
    new_run_id,
    pid_alive,
    run_dir,
)

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
        help="both agents read-only; nothing on disk is modified "
        "(with --clean: only list what would go)",
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
        "--clean", action="store_true", help="delete the runs saved for this directory"
    )
    parser.add_argument(
        "--all", action="store_true", help="with --clean or --runs: every directory's runs"
    )
    # A required value, unlike the optional one the note on --merge warns
    # about: it takes exactly the next word, so it cannot swallow the task.
    parser.add_argument(
        "--older-than",
        type=_days,
        metavar="DAYS",
        help="with --clean: keep the runs newer than this",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="with --clean: do not ask first"
    )
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


def _days(text: str) -> float:
    try:
        days = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of days: {text!r}") from None
    if not 0 <= days < float("inf"):
        raise argparse.ArgumentTypeError(f"not a number of days: {text!r}")
    return days


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.older_than is not None or args.yes) and not args.clean:
        parser.error("--older-than and --yes only mean something with --clean")
    if args.all and not (args.clean or args.runs):
        parser.error("--all only means something with --clean or --runs")
    cwd = (args.cwd or Path.cwd()).resolve()

    if args.demo:
        # Before the engines are built, so it runs with neither CLI installed —
        # which is the state of the user most likely to want it.
        from dai import demo

        return demo.run(cwd, appearance=_appearance(args.theme or "auto"))
    if args.init:
        try:
            # An explicit --config names the file to write, and nothing moves.
            moved = None if args.config else config_module.migrate_legacy()
            path, added = config_module.ensure_config(args.config)
        except OSError as exc:
            print(f"dai: cannot write the config: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"config: {path}")
        if moved is not None:
            print(f"  copied from {moved} — no longer read; delete it when you like")
        for name in added:
            print(f"  added {name}")
        return EXIT_OK
    if args.clean:
        # Before the is_dir() guard: a project whose directory is gone is
        # exactly the one whose runs are left over.
        return _clean(
            cwd, everywhere=args.all, older_than=args.older_than,
            yes=args.yes, dry_run=args.dry_run,
        )
    if args.runs:
        return _list_runs(cwd, everywhere=args.all)
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

    # Minted here rather than after the prompt: a screenshot pasted into the
    # task goes into this run's own directory, which makes it the earliest
    # thing dai writes anywhere. The call itself creates nothing.
    run_id = new_run_id()
    images = run_dir(cwd, run_id) / "images"

    task = _resolve_task(args)
    if not task and interactive:
        from dai.tui import ask_for_task

        task = ask_for_task(
            cwd,
            debounce_ms=cfg.completion_debounce_ms,
            appearance=appearance,
            images_dir=images,
        )
    if not task:
        print('dai: give me a task, e.g. dai "fill in the table in docs/matrix.md"',
              file=sys.stderr)
        return EXIT_ERROR

    try:
        solver, critic = _build_engines(cfg, writing=not args.dry_run, read_dirs=(images,))
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


def _build_engines(
    cfg: Config, *, writing: bool, read_dirs: tuple[Path, ...] = ()
) -> tuple[Engine, Engine]:
    """Both engines. `read_dirs` is what they may read outside the workdir.

    The run's pasted screenshots, today: they live under `~/.dai`, which is
    outside the directory either agent is started in.
    """

    solver_cfg = cfg.engine(cfg.solver)
    critic_cfg = cfg.engine(cfg.critic)
    solver = build_engine(
        cfg.solver,
        cmd=solver_cfg.cmd or None,
        model=solver_cfg.model,
        extra_args=solver_cfg.args_for(writing=writing),
        read_dirs=read_dirs,
    )
    critic = build_engine(
        cfg.critic,
        cmd=critic_cfg.cmd or None,
        model=critic_cfg.model,
        extra_args=critic_cfg.args_for(writing=False),
        read_dirs=read_dirs,
    )
    return solver, critic


def _missing_binaries(*engines: Engine) -> list[str]:
    return sorted({e.cmd for e in engines if shutil.which(e.cmd) is None})


# --- past runs ------------------------------------------------------------


def _list_runs(cwd: Path, *, everywhere: bool = False) -> int:
    runs = list_all_runs() if everywhere else list_runs(cwd)
    if not runs:
        print("no runs recorded" if everywhere else "no runs recorded here")
    for info in runs:
        outcome = info.outcome or ("running" if pid_alive(info.pid) else "unfinished")
        where = f"  {_colour(_tilde(info.cwd), DIM)}" if everywhere else ""
        print(f"{info.run_id}  {outcome:<10}{where}  {_clip(info.task)}")
    if (cwd / ".dai" / "runs").is_dir():
        print(_colour(
            "older runs in ./.dai/runs are not listed — `dai --clean` removes them", DIM
        ))
    return EXIT_OK


def _clean(cwd: Path, *, everywhere: bool, older_than: float | None,
           yes: bool, dry_run: bool) -> int:
    """Delete saved runs: list them, ask, delete. Never a run still going."""

    found = cleanup.plan(cwd, everywhere=everywhere, older_than=older_than)

    # Newest first within each directory, and the old layout last. Two sorts,
    # because the second is stable and keeps the order the first one made.
    rows = sorted(found.remove + found.running, key=lambda c: c.run_id, reverse=True)
    rows.sort(key=lambda c: (c.legacy, c.where))
    kept = {c.path for c in found.running}
    heading = None
    for candidate in rows:
        if candidate.where != heading:
            heading = candidate.where
            print(_tilde(heading))
        if candidate.path in kept:
            why = (
                f"still running (pid {candidate.pid})"
                if candidate.pid is not None
                else "touched in the last day, may be in use"
            )
            print(_colour(f"  ! {candidate.run_id}  {why} — kept", YELLOW))
            continue
        outcome = candidate.outcome or ("unfinished" if candidate.recorded else "abandoned")
        print(
            f"  {candidate.run_id}  {outcome:<10}  "
            f"{_colour(f'{_size(candidate.size):>7}', DIM)}  {_clip(candidate.task)}"
        )
    if found.newer:
        print(_colour(f"{_runs(found.newer)} newer than {older_than:g} days kept", DIM))

    if not found.remove:
        print("nothing to clean")
        return EXIT_OK
    total = f"{_runs(len(found.remove))}, {_size(found.size)}"
    if dry_run:
        print(_colour(f"{total} — dry run, nothing deleted", DIM))
        return EXIT_OK

    if not yes:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            sys.stdout.flush()  # the list first, then why nothing happened to it
            print(
                "dai: nobody to confirm with — pass --yes to delete, or --dry-run to look",
                file=sys.stderr,
            )
            return EXIT_ERROR
        try:
            answer = input(f"delete {total}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer not in ("y", "yes"):
            print("nothing deleted")
            return EXIT_OK

    freed, failures = cleanup.remove(found)
    for path, reason in failures:
        print(f"dai: could not delete {path}: {reason}", file=sys.stderr)
    print(f"removed {_runs(len(found.remove) - len(failures))}, {_size(freed)}")
    return EXIT_ERROR if failures else EXIT_OK


def _runs(number: int) -> str:
    return f"{number} run" if number == 1 else f"{number} runs"


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _clip(task: str, width: int = 60) -> str:
    task = " ".join(task.split())
    return task if len(task) <= width else task[: width - 1] + "…"


def _tilde(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


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
    directory = find_run(run_id, cwd)
    if directory is None:
        print(f"dai: no run {run_id}", file=sys.stderr)
        return EXIT_ERROR
    report = directory / "report.md"
    if not report.is_file():
        print(
            f"dai: run {run_id} has no report — it never finished; "
            f"its log is {directory / 'events.jsonl'}",
            file=sys.stderr,
        )
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
            which = (
                "extra round"
                if debate.is_extra(event.round)
                else f"round {debate.counted_round(event.round)}"
            )
            print(
                f"{_colour('▸', BLUE)} {which} · "
                f"{_colour(event.engine, BOLD)} {label}"
            )
        elif event.kind == "objection":
            print()
            print(f"{_colour('▸', YELLOW)} extra round · your note: {event.text}")
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

    # The same loop as the TUI's: an objection instead of a merge buys the
    # agents one extra round, and the question comes back only if they agree.
    while True:
        if snapshotter.active:
            final = await asyncio.to_thread(
                snapshotter.capture_final, f"{result.outcome.value}: {result.reason}"
            )
            transcript.snapshots(final)
        note = None
        if (
            snapshotter.active
            and result.agreed
            and snapshotter.settings.merge is not Merge.NEVER
        ):
            note = await _settle_merge(
                snapshotter, snapshotter.settings.merge, cwd, debate
            )
        if not note:
            break
        snapshotter.mark()
        result = await debate.overrule(note)

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


async def _settle_merge(
    snapshotter: Snapshotter, merge: Merge, cwd: Path, debate: Debate | None = None
) -> str | None:
    """Decide what goes home, and move it. Only ever called on agreement.

    `always` is the old behaviour untouched. `ask` shows what each repository
    would write and takes an answer — but only if there is somebody to answer:
    piped or redirected, nothing is merged and the record says why, which is
    the same way `deadlock_policy = "ask"` degrades with nobody to ask.

    Given the debate, the answer may also be an objection: then nothing is
    merged, and the note comes back for the caller to send the agents.
    """

    if merge is Merge.ALWAYS:
        await asyncio.to_thread(snapshotter.merge)
        return None

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        await asyncio.to_thread(
            snapshotter.merge, only=(), kept="no terminal to ask on"
        )
        return None

    rows = await asyncio.to_thread(snapshotter.preview)
    if not rows:
        return None

    ask = {}
    if debate is not None:
        ask = {
            "can_object": True,
            "blocked": debate.objection_blocked() or "",
            "recap": debate.objection_recap(),
        }
        # Nobody is spending anything while a person reads the diff.
        debate.budget.hold()
    try:
        chosen = await asyncio.to_thread(lambda: _ask_merge(rows, **ask))
    finally:
        if debate is not None:
            debate.budget.release()
    if isinstance(chosen, Objection):
        return chosen.note
    await asyncio.to_thread(
        snapshotter.merge, only=chosen, kept="you kept the branch"
    )
    return None


def _ask_merge(
    rows: list[MergeCandidate],
    *,
    can_object: bool = False,
    blocked: str = "",
    recap: ObjectionRecap | None = None,
) -> list[Path] | Objection:
    """Show what would be written, and ask. All of it or none of it.

    Deliberately not a picker: a selector built out of raw stdin is worse than
    an honest yes or no, and the one that can pick repository by repository is
    the TUI. Anything but yes keeps every branch, which is also what an
    unreadable stdin and an interrupt mean.

    With `can_object`, `o` is a third answer: one line of note, and the agents
    go back for an extra round instead. An empty note objects to nothing, so
    the question is simply put again.
    """

    print()
    if recap is not None:
        verdict = {
            "addressed": _colour("critic: addressed ✓", GREEN),
            "open": _colour("critic: still open", YELLOW),
            "dismissed": _colour("you dismissed it", DIM),
        }.get(recap.status, "")
        print(f"{_colour('extra round · your note', YELLOW)} — {verdict}")
        print(f"    {_colour(recap.answer or recap.note, DIM)}")
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
        if not row.refusal and row.since is not None and any(row.since):
            plus, minus = row.since
            bought = " ".join(
                part for part in (f"+{plus}" if plus else "", f"-{minus}" if minus else "")
                if part
            )
            detail = f"{_colour(f'{bought} from your round', YELLOW)} · " + _colour(
                detail, DIM
            )
            print(f"    {detail}")
        else:
            print(f"    {_colour(detail, YELLOW if row.refusal else DIM)}")

    ready = [row.repo for row in rows if row.mergeable]
    if not ready:
        print(_colour("none of them can be merged — the branches stay", DIM))
        return []

    if can_object and blocked:
        print(_colour(f"objecting is not available — {blocked}", DIM))
        can_object = False
    elif can_object:
        print(
            _colour(
                "o objects instead: one extra round with a note of yours, "
                "outside the round limit",
                DIM,
            )
        )

    choices = "[y/N/o]" if can_object else "[y/N]"
    while True:
        try:
            answer = input(
                f"merge {_repos(len(ready))} into their base branches? {choices} "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not (can_object and answer in ("o", "object")):
            return ready if answer in ("y", "yes") else []
        try:
            note = input("what should they fix before merge? ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if note:
            return Objection(note)


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
