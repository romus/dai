"""Where dai keeps its own things: `~/.dai`, and never the directory it works in.

Everything dai writes for itself — the config, every run's event log, its
report and the screenshots pasted into it — lives here, so a project dai has
argued over carries no trace of it: nothing to ignore, nothing to hide from
git, nothing swept into a commit.

A leaf on purpose: `config`, `transcript` and `cleanup` all need it, and it
needs nothing from them.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

ENV = "DAI_HOME"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def root() -> Path:
    """`$DAI_HOME`, or `~/.dai`.

    Read on every call rather than once at import, so a test that moves it —
    and every test does, since none may touch the real one — is believed.
    """

    override = os.environ.get(ENV)
    if override:
        return Path(override).expanduser().absolute()
    return Path.home() / ".dai"


def projects_dir() -> Path:
    return root() / "projects"


def project_slug(workdir: Path) -> str:
    """One directory name per working directory, readable and collision-free.

    The readable part is the path with everything outside `[A-Za-z0-9._-]`
    folded to `-`, keeping the tail when it is long, since the project's name is
    at the end. That alone collides — `/a/b-c` and `/a/b/c` fold to the same
    string, and two Cyrillic paths of one length fold to the same row of dashes
    — and `--clean` deletes by project, so a collision would delete somebody
    else's runs. Hence the hash of the real path, always.
    """

    path = os.path.realpath(workdir)
    readable = _UNSAFE.sub("-", path)[-80:].lstrip("-.") or "root"
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:8]
    return f"{readable}-{digest}"


def project_dir(workdir: Path) -> Path:
    return projects_dir() / project_slug(workdir)
