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


def _replies(*pairs: tuple[str, str]) -> list[dict]:
    return [
        {"id": i, "action": a, "detail": "the README is unambiguous about this"}
        for i, a in pairs
    ]


SOLVES = [
    _solved("filled in every empty cell from the README"),
    _solved(
        "corrected the port and filled the status column",
        _replies(("i1", "FIXED"), ("i2", "FIXED")),
    ),
]

CRITIQUES = [
    {
        "verdict": "REQUEST_CHANGES",
        "checked": [f"read {SUBJECT}", "read README.md"],
        "issues": [
            {
                "id": "i1",
                "severity": "major",
                "claim": "the port given for auth contradicts the README",
                "evidence": f"{SUBJECT}:5",
                "fix": "8080, as the README says",
            },
            {
                "id": "i2",
                "severity": "minor",
                "claim": "the status column is still empty for search",
                "evidence": f"{SUBJECT}:6",
                "fix": "beta, not yet GA",
            },
        ],
        "conceded": [],
        "summary": "two cells disagree with the source",
    },
    {
        "verdict": "APPROVE",
        "checked": [f"read {SUBJECT}", "compared every cell against README.md"],
        "issues": [],
        "conceded": [],
        "summary": "every cell now matches the source",
    },
]
