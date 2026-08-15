"""Core data types shared across dai."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Access(str, Enum):
    """What an agent is allowed to do to the working tree.

    The solver writes, the critic reads. Keeping this a type rather than a bare
    flag means an engine cannot silently be spawned with the wrong permissions.
    """

    WRITE = "write"
    READ_ONLY = "read_only"


class Role(str, Enum):
    """Which move in the argument an agent is making."""

    SOLVE = "solve"
    CRITIQUE = "critique"
    REBUT = "rebut"


@dataclass(frozen=True)
class Usage:
    """Token counts for one engine call.

    Cache reads are tracked apart from fresh input because they are billed at a
    different rate — collapsing them would make cost estimates wrong.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class AgentEvent:
    """Something an agent did, surfaced to the UI while it is still working.

    `kind` is one of: text, thinking, tool, tool_result, status, error.
    """

    kind: str
    text: str = ""
    detail: str = ""


@dataclass
class TurnResult:
    """Outcome of a single engine invocation."""

    text: str = ""
    structured: dict | None = None
    session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    error: str | None = None
    duration_s: float = 0.0
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


class Severity(str, Enum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"

    @property
    def rank(self) -> int:
        return {"blocker": 3, "major": 2, "minor": 1}[self.value]


class Verdict(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class Action(str, Enum):
    """How the solver answered one of the critic's issues."""

    FIXED = "FIXED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Issue:
    """One complaint from the critic."""

    id: str
    severity: Severity
    claim: str
    evidence: str = ""
    fix: str = ""

    @property
    def fingerprint(self) -> str:
        """Identity of the *complaint*, independent of how it was worded.

        Used to notice that the critic is re-raising the same point round after
        round. Ids are unreliable for this — models renumber freely — so the
        claim text itself, normalised, is what we compare.
        """

        return " ".join(self.claim.lower().split())[:200]


@dataclass(frozen=True)
class Reply:
    """The solver's answer to one issue."""

    id: str
    action: Action
    detail: str = ""


@dataclass
class CriticTurn:
    """A parsed critique."""

    verdict: Verdict
    issues: list[Issue] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    conceded: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def open_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.id not in set(self.conceded)]

    @property
    def worst(self) -> Severity | None:
        return max((i.severity for i in self.open_issues), key=lambda s: s.rank, default=None)


@dataclass
class SolverTurn:
    """A parsed solver move — either the initial attempt or a rebuttal."""

    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    replies: list[Reply] = field(default_factory=list)

    def reply_to(self, issue_id: str) -> Reply | None:
        return next((r for r in self.replies if r.id == issue_id), None)

    @property
    def rejected_ids(self) -> set[str]:
        return {r.id for r in self.replies if r.action is Action.REJECTED}


class Outcome(str, Enum):
    """Why the argument stopped."""

    CONSENSUS = "consensus"
    DEADLOCK = "deadlock"
    BUDGET = "budget"
    ROUNDS = "rounds"
    ABORTED = "aborted"
    FAILED = "failed"
