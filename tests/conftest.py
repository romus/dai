import sys
from pathlib import Path

import pytest

# Allow `pytest` to work straight from a checkout, without an install step.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def dai_home(tmp_path_factory, monkeypatch):
    """Every test gets its own `~/.dai`, and none ever touches the real one.

    A sibling of `tmp_path`, never inside it: plenty of tests build a repo right
    in `tmp_path`, and a home inside it would be swept into their commits.
    Not created, so a test can assert that nothing was written at all. `HOME`
    itself is left alone — git reads `~/.gitconfig` in the snapshot tests.
    """

    base = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("DAI_HOME", str(base / ".dai"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / ".config"))
    return base / ".dai"
