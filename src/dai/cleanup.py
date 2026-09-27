"""Deleting saved runs: `dai --clean`.

Split in two on purpose. `plan()` only looks — it decides what would go, what
stays and why, and touches nothing — so the command can show the list, ask,
and offer `--dry-run` without a second code path. `remove()` then deletes
exactly what it was handed, and never raises: a run it could not delete is
reported, not fatal.

Two things are never deleted: a run that may still be going, and anything that
is not a run directory — `config.toml` above all.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dai import home
from dai.transcript import pid_alive, read_run, run_started, runs_root

#: How long a directory with no recorded pid counts as possibly in use. A
#: screenshot pasted at the task prompt makes the run's directory before any
#: event — and so any pid — exists; deleting it from a second terminal would
#: take the picture the open prompt is about to send.
GRACE = timedelta(days=1)


@dataclass
class Candidate:
    path: Path
    run_id: str
    #: The heading the run is listed under: the directory it worked in.
    where: str
    legacy: bool
    task: str
    outcome: str
    started: datetime
    size: int
    pid: int | None
    #: False for a directory with no event log: a prompt backed out of after a
    #: screenshot was pasted into it, or a run that died before saying anything.
    recorded: bool = True


@dataclass
class Plan:
    remove: list[Candidate] = field(default_factory=list)
    #: Kept because they may still be going.
    running: list[Candidate] = field(default_factory=list)
    #: How many were kept for being newer than `--older-than`.
    newer: int = 0

    @property
    def size(self) -> int:
        return sum(c.size for c in self.remove)


def plan(
    workdir: Path,
    *,
    everywhere: bool = False,
    older_than: float | None = None,
    now: datetime | None = None,
    alive: Callable[[object], bool] = pid_alive,
) -> Plan:
    """What `--clean` would delete, and what it would keep. Touches nothing.

    Looks at this directory's runs under `~/.dai` — or every project's, with
    `everywhere` — and always at `<workdir>/.dai/runs` as well, where runs
    were kept before they moved home.
    """

    now = now or datetime.now()
    result = Plan()
    for base, where, legacy in _sources(workdir, everywhere):
        for directory in sorted(_run_dirs(base), key=lambda d: d.name, reverse=True):
            candidate = _candidate(directory, where, legacy)
            if _may_be_running(candidate, directory, now, alive):
                result.running.append(candidate)
            elif older_than is not None and now - candidate.started < timedelta(days=older_than):
                result.newer += 1
            else:
                result.remove.append(candidate)
    return result


def remove(chosen: Plan) -> tuple[int, list[tuple[Path, str]]]:
    """Delete what `plan()` chose. Returns bytes freed and what failed.

    Afterwards the directories that held them go too, but only if that left
    them empty — never `projects/` or `~/.dai` itself, so never the config.
    """

    freed, failures = 0, []
    emptied: dict[Path, None] = {}
    for candidate in chosen.remove:
        try:
            shutil.rmtree(candidate.path)
        except OSError as exc:
            failures.append((candidate.path, exc.strerror or str(exc)))
            continue
        freed += candidate.size
        runs = candidate.path.parent
        # runs/ and the project above it; for the old layout, .dai/runs and .dai.
        emptied[runs] = None
        emptied[runs.parent] = None
    for directory in emptied:
        try:
            directory.rmdir()
        except OSError:
            pass
    return freed, failures


# --- internals ------------------------------------------------------------


def _sources(workdir: Path, everywhere: bool) -> list[tuple[Path, str, bool]]:
    workdir = Path(workdir)
    if everywhere:
        found = [
            (project / "runs", _label(project), False)
            for project in sorted(_subdirs(home.projects_dir()))
        ]
    else:
        found = [(runs_root(workdir), str(workdir), False)]
    found.append((workdir / ".dai" / "runs", f"{workdir}/.dai/runs (old layout)", True))
    return found


def _label(project: Path) -> str:
    """The directory a project's runs worked in, as they recorded it."""

    for directory in _run_dirs(project / "runs"):
        info = read_run(directory)
        if info is not None and info.cwd:
            return info.cwd
    return project.name


def _candidate(directory: Path, where: str, legacy: bool) -> Candidate:
    info = read_run(directory)
    return Candidate(
        path=directory,
        run_id=directory.name,
        where=where,
        legacy=legacy,
        task=info.task if info else "",
        outcome=info.outcome if info else "",
        started=run_started(directory.name, directory),
        size=_size(directory),
        pid=info.pid if info else None,
        recorded=info is not None,
    )


def _may_be_running(candidate: Candidate, directory: Path, now: datetime,
                    alive: Callable[[object], bool]) -> bool:
    if candidate.outcome:
        return False
    if candidate.pid is not None:
        return alive(candidate.pid)
    return now - _last_touched(directory) < GRACE


def _run_dirs(base: Path) -> list[Path]:
    # Real directories only: a symlink in here is not something we made, and
    # following it would delete wherever it points.
    return [d for d in _subdirs(base) if not d.is_symlink()]


def _subdirs(base: Path) -> list[Path]:
    try:
        return [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return []


def _size(directory: Path) -> int:
    total = 0
    for top, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                pass
    return total


def _last_touched(directory: Path) -> datetime:
    newest = 0.0
    for top, _dirs, files in os.walk(directory):
        for name in (".", *files):
            try:
                newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime)
            except OSError:
                pass
    return datetime.fromtimestamp(newest)
