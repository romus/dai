"""Canned arguments, one per ending worth looking at.

The point of each is a screen: `deadlock-ask` exists to put `DeadlockScreen` in
front of you, `agree` to reach the merge dialog. They are written against the
referee's real rules rather than around them —

* an `APPROVE` whose `checked` is empty is rejected outright, and one whose
  prose names none of the solver's changed files costs the run its one `PROVE`
  turn, so an approval meant to land says which file it read;
* a deadlock needs the *same* claim back for `no_progress_rounds` rounds, since
  issues are fingerprinted by their normalised claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The file the fake solver edits, and the one its critic must be seen to read.
SUBJECT = "notes.md"


def solved(summary: str = "filled in the table", responses=()) -> dict:
    return {
        "summary": summary,
        "files_changed": [SUBJECT],
        "responses": list(responses),
    }


def approve(summary: str = "matches the source", checked=(f"read {SUBJECT}",)) -> dict:
    return {
        "verdict": "APPROVE",
        "checked": list(checked),
        "issues": [],
        "conceded": [],
        "summary": summary,
    }


def changes(*claims: str, severity: str = "major", conceded=()) -> dict:
    return {
        "verdict": "REQUEST_CHANGES",
        "checked": [f"read {SUBJECT}"],
        "issues": [
            {
                "id": f"i{n}",
                "severity": severity,
                "claim": claim,
                "evidence": f"{SUBJECT}:{n + 3}",
                "fix": "say what it should be instead",
            }
            for n, claim in enumerate(claims, start=1)
        ],
        "conceded": list(conceded),
        "summary": "not there yet",
    }


def rebut(*pairs: tuple[str, str]) -> list[dict]:
    return [{"id": i, "action": a, "detail": "because the source says so"} for i, a in pairs]


@dataclass(frozen=True)
class Scenario:
    """One canned argument, and how the run has to be set up to reach it."""

    summary: str
    solves: list[dict] = field(default_factory=list)
    critiques: list[dict] = field(default_factory=list)
    #: Overrides the run's deadlock policy, for the scenario that needs asking.
    policy: str = ""
    #: What one fake turn claims to cost, for walking a run out of money.
    cost: float = 0.01
    max_rounds: int = 5
    max_usd: float | None = None


_STALLED = "the status column is still empty for search"

SCENARIOS: dict[str, Scenario] = {
    "agree": Scenario(
        summary="two rounds, then the critic signs it off",
        solves=[solved(), solved("addressed both", rebut(("i1", "FIXED"), ("i2", "FIXED")))],
        critiques=[changes("the port for auth is wrong", "the status column is empty"), approve()],
    ),
    "quick": Scenario(
        summary="approved on the first round — straight to the merge dialog",
        solves=[solved()],
        critiques=[approve()],
    ),
    "deadlock": Scenario(
        summary="neither side moves; the configured policy decides",
        solves=[solved()] + [solved("stands", rebut(("i1", "REJECTED"))) for _ in range(4)],
        critiques=[changes(_STALLED) for _ in range(5)],
    ),
    "deadlock-ask": Scenario(
        summary="the same stall, but you are asked who wins",
        solves=[solved()] + [solved("stands", rebut(("i1", "REJECTED"))) for _ in range(4)],
        critiques=[changes(_STALLED) for _ in range(5)],
        policy="ask",
    ),
    "rounds": Scenario(
        summary="runs out of rounds with the argument still open",
        solves=[solved()] + [solved("partly", rebut(("i1", "PARTIAL"))) for _ in range(3)],
        critiques=[changes(f"cell {n} is still wrong") for n in range(1, 5)],
        max_rounds=2,
    ),
    "budget": Scenario(
        summary="stops because the next round is unaffordable",
        solves=[solved()] + [solved("partly", rebut(("i1", "PARTIAL"))) for _ in range(3)],
        critiques=[changes(f"cell {n} is still wrong") for n in range(1, 5)],
        cost=0.4,
        max_usd=1.0,
    ),
    "rubber-stamp": Scenario(
        summary="an approval that checked nothing, and the PROVE re-ask it earns",
        solves=[solved(), solved()],
        critiques=[approve(checked=()), approve()],
    ),
}


def describe() -> list[str]:
    width = max(len(name) for name in SCENARIOS)
    return [f"  {name:<{width}}  {s.summary}" for name, s in SCENARIOS.items()]
