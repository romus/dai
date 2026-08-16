"""The argument the demo plays: two rounds, then agreement.

Written against the referee's real rules rather than around them — an approval
whose `checked` is empty is rejected outright, and one whose prose names none of
the solver's changed files costs the run a turn being challenged. So the critic
here says which file it read, and it is the file the solver says it changed.
"""

from __future__ import annotations

#: What the imaginary solver edits, and the imaginary critic is seen to read.
SUBJECT = "matrix.md"

TASK = "fill in the empty cells of the table in matrix.md, using README.md as the source"


def _solved(summary: str, responses=()) -> dict:
    return {"summary": summary, "files_changed": [SUBJECT], "responses": list(responses)}


def _replies(*triples: tuple[str, str, str]) -> list[dict]:
    """`detail` is what the SOLVER SAYS panel renders, so it carries the argument."""

    return [{"id": i, "action": a, "detail": d} for i, a, d in triples]


def _stuck() -> dict:
    """The same two complaints, filed again. Three of these stall the referee."""

    return {
        "verdict": "REQUEST_CHANGES",
        "checked": [f"read {SUBJECT}", "read README.md"],
        "issues": [
            {
                "id": "i1",
                "severity": "major",
                "claim": "The status column is still empty for search.",
                "evidence": f"{SUBJECT}:6",
                "fix": "write 'beta' there; the spec says every row is filled",
            },
            {
                "id": "i2",
                "severity": "minor",
                "claim": "The port given for auth contradicts the README.",
                "evidence": f"{SUBJECT}:5",
                "fix": "8080, as the README says",
            },
        ],
        "conceded": [],
        "summary": "two cells the solver will not change",
    }


SOLVES = [
    _solved("filled in every empty cell I could source from the README"),
    # The rebuttals below are what the SOLVER SAYS panel shows you at the
    # deadlock, so they are written as arguments rather than as filler.
    _solved(
        "these two are already right",
        _replies(
            ("i1", "REJECTED", "Search has no status to show. Empty is the correct state."),
            ("i2", "REJECTED", "The README says 8080 for auth, and 8080 is what the table says."),
        ),
    ),
    _solved(
        "still right, and for the same reasons",
        _replies(
            ("i1", "REJECTED", "Search has no status to show. Empty is the correct state."),
            ("i2", "REJECTED", "The README says 8080 for auth, and 8080 is what the table says."),
        ),
    ),
    # After the ruling: whatever was upheld gets applied without argument.
    _solved("applied what you upheld", _replies(("i1", "FIXED", "wrote 'beta' at line 6"))),
]

CRITIQUES = [
    _stuck(),
    _stuck(),
    _stuck(),
    {
        "verdict": "APPROVE",
        "checked": [f"read {SUBJECT}", "compared every cell against README.md"],
        "issues": [],
        "conceded": [],
        "summary": "every cell now matches the source",
    },
]
