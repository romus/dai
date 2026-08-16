"""The argument loop, driven by scripted engines instead of real CLIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from dai.budget import Budget, Limits
from dai.consensus import Referee
from dai.engines.base import Engine
from dai.models import Outcome, Role, TurnResult, Usage
from dai.orchestrator import Debate


class Scripted(Engine):
    """An engine that replays canned structured answers.

    A string in the script means "fail this turn with that message".
    """

    def __init__(self, name: str, script: list, cost: float = 0.01):
        super().__init__(cmd="/bin/true")
        self.name = name
        self.script = list(script)
        self.cost = cost
        self.prompts: list[str] = []
        self.accesses: list = []

    def build_argv(self, prompt, **_):  # pragma: no cover - never spawned
        return ["/bin/true"]

    def handle_event(self, event, turn):  # pragma: no cover - never spawned
        pass

    async def run(self, prompt, *, cwd=None, access=None, schema=None, session=None,
                  on_event=None, timeout=None):
        self.prompts.append(prompt)
        self.accesses.append(access)
        if not self.script:
            return TurnResult(error=f"{self.name}: script exhausted")
        item = self.script.pop(0)
        if isinstance(item, str):
            return TurnResult(error=item)
        return TurnResult(
            structured=item,
            text="ok",
            cost_usd=self.cost,
            usage=Usage(output_tokens=10),
            session_id="s1",
        )


def solved(summary="did it", files=("a.md",), responses=()):
    return {"summary": summary, "files_changed": list(files), "responses": list(responses)}


def replies(*pairs):
    return [{"id": i, "action": a, "detail": "because"} for i, a in pairs]


def approve(checked=("read a.md",)):
    return {"verdict": "APPROVE", "checked": list(checked), "issues": [],
            "conceded": [], "summary": "fine"}


def changes(*claims, ids=None, severity="major", evidence="a.md:1", conceded=()):
    ids = ids or [f"i{n}" for n in range(1, len(claims) + 1)]
    return {
        "verdict": "REQUEST_CHANGES",
        "checked": ["read a.md"],
        "issues": [
            {"id": i, "severity": severity, "claim": c, "evidence": evidence, "fix": "do it"}
            for i, c in zip(ids, claims)
        ],
        "conceded": list(conceded),
        "summary": "needs work",
    }


def debate(tmp_path: Path, solver_script, critic_script, **kwargs):
    solver = Scripted("solver", solver_script)
    critic = Scripted("critic", critic_script)
    kwargs.setdefault("budget", Budget(Limits(max_rounds=5, max_usd=None, max_wall_seconds=None)))
    return Debate(task="do the thing", cwd=tmp_path, solver=solver, critic=critic, **kwargs), solver, critic


# --- happy paths ----------------------------------------------------------


async def test_immediate_verified_approval_ends_in_one_round(tmp_path):
    d, solver, critic = debate(tmp_path, [solved()], [approve()])

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS
    assert result.agreed
    assert len(result.rounds) == 1
    assert len(solver.prompts) == 1


async def test_one_round_of_pushback_then_agreement(tmp_path):
    d, solver, critic = debate(
        tmp_path,
        [solved(), solved(responses=replies(("i1", "FIXED")))],
        [changes("empty column"), approve()],
    )

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS
    assert len(result.rounds) == 2
    assert "REJECTED" in solver.prompts[1], "the rebuttal prompt must allow refusal"


async def test_the_solver_writes_and_the_critic_only_reads(tmp_path):
    d, solver, critic = debate(tmp_path, [solved()], [approve()])

    await d.run()

    assert all(a.value == "write" for a in solver.accesses)
    assert all(a.value == "read_only" for a in critic.accesses)


async def test_an_unaudited_approval_is_sent_back_to_the_critic(tmp_path):
    """APPROVE with nothing checked must cost the critic a turn, not the solver.

    It used to cost the solver one: with no open issues to rebut, the next turn
    was a write-access agent told to make fixes over an empty list.
    """

    d, solver, critic = debate(tmp_path, [solved()], [approve(checked=()), approve()])

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS
    assert len(critic.prompts) == 2
    assert "was not accepted" in critic.prompts[1]
    assert len(solver.prompts) == 1, "the solver must not be asked to fix nothing"
    assert any("what it checked" in n for n in result.rounds[0].notes)


async def test_a_critic_that_will_not_substantiate_never_reaches_the_solver(tmp_path):
    """Asked twice and still showing nothing: that is not agreement to act on."""

    d, solver, critic = debate(
        tmp_path, [solved()], [approve(checked=()), approve(checked=())]
    )

    result = await d.run()

    assert result.outcome is Outcome.DEADLOCK
    assert "would not substantiate" in result.reason
    assert len(solver.prompts) == 1


async def test_an_approval_naming_no_changed_file_is_challenged_once(tmp_path):
    """The soft rule may cost a turn; it may never cost the run."""

    d, solver, critic = debate(
        tmp_path,
        [solved()],
        [approve(checked=("skimmed the diff",)), approve(checked=("skimmed it again",))],
    )

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS, "prose we cannot parse is not a verdict"
    assert len(critic.prompts) == 2
    assert "naming any file the solver changed" in critic.prompts[1]


async def test_the_next_critique_is_not_shown_issues_it_conceded(tmp_path):
    """A conceded issue rendered back reads as "NO ANSWER" against the critic's
    own concession — it re-raises, the open set churns, and deadlock never fires."""

    d, solver, critic = debate(
        tmp_path,
        [solved(), solved(responses=replies(("i1", "FIXED")))],
        [changes("first", "second", ids=["i1", "i2"], conceded=["i2"]), approve()],
    )

    await d.run()

    assert "NO ANSWER" not in critic.prompts[1]
    assert "second" not in critic.prompts[1]


async def test_rigor_reaches_both_agents(tmp_path):
    """The user asked for a harsher review; both sides have to hear about it."""

    d, solver, critic = debate(tmp_path, [solved()], [approve()], rigor="brutal")

    await d.run()

    assert "Depth for this run: maximum" in solver.prompts[0]
    assert "Depth for this run: maximum" in critic.prompts[0]


# --- deadlock -------------------------------------------------------------


async def test_repetition_deadlocks_and_the_critic_prevails_by_default(tmp_path):
    stuck = [changes("same complaint") for _ in range(4)]
    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved(responses=replies(("i1", "REJECTED"))) for _ in range(4)],
        stuck,
        referee=Referee(no_progress_rounds=2),
        deadlock_policy="critic",
    )

    result = await d.run()

    assert result.outcome is Outcome.DEADLOCK
    assert "critic prevailed" in result.reason
    assert "FINAL round" in solver.prompts[-1]


async def test_solver_policy_leaves_the_work_as_it_stands(tmp_path):
    stuck = [changes("same complaint") for _ in range(4)]
    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved(responses=replies(("i1", "REJECTED"))) for _ in range(4)],
        stuck,
        referee=Referee(no_progress_rounds=2),
        deadlock_policy="solver",
    )

    result = await d.run()

    assert result.outcome is Outcome.DEADLOCK
    assert "solver's version stands" in result.reason
    assert "FINAL round" not in solver.prompts[-1]
    assert result.open_issues, "the unresolved objections must be reported"


async def test_ask_policy_defers_to_the_host(tmp_path):
    asked = []

    async def decide(pending):
        asked.append(pending.outcome)
        return "solver"

    stuck = [changes("same complaint") for _ in range(4)]
    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved(responses=replies(("i1", "REJECTED"))) for _ in range(4)],
        stuck,
        referee=Referee(no_progress_rounds=2),
        deadlock_policy="ask",
        on_deadlock=decide,
    )

    result = await d.run()

    assert asked == [Outcome.DEADLOCK]
    assert "solver's version stands" in result.reason


# --- limits ---------------------------------------------------------------


async def test_round_limit_is_not_overrun(tmp_path):
    """max_rounds=3 must mean three critiques, not four."""

    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved(responses=replies((f"i{n}", "REJECTED"))) for n in range(1, 6)],
        [changes(f"complaint {n}", ids=[f"i{n}"]) for n in range(1, 6)],
        budget=Budget(Limits(max_rounds=3, max_usd=None, max_wall_seconds=None)),
        deadlock_policy="solver",
    )

    result = await d.run()

    assert result.outcome is Outcome.ROUNDS
    assert len(critic.prompts) == 3


async def test_running_out_of_money_stops_the_argument(tmp_path):
    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved() for _ in range(5)],
        [changes(f"complaint {n}", ids=[f"i{n}"]) for n in range(1, 6)],
        budget=Budget(Limits(max_rounds=99, max_usd=0.05, max_wall_seconds=None)),
        deadlock_policy="solver",
    )

    result = await d.run()

    assert result.outcome is Outcome.BUDGET
    assert "budget" in result.reason


async def test_critic_objections_are_applied_even_with_no_rounds_left(tmp_path):
    """Out of rounds is how deadlock happens; it must not block the last turn."""

    d, solver, critic = debate(
        tmp_path,
        [solved()] + [solved(responses=replies((f"i{n}", "REJECTED"))) for n in range(1, 5)],
        [changes(f"complaint {n}", ids=[f"i{n}"]) for n in range(1, 5)],
        budget=Budget(Limits(max_rounds=2, max_usd=None, max_wall_seconds=None)),
        deadlock_policy="critic",
    )

    result = await d.run()

    assert result.outcome is Outcome.ROUNDS
    assert "critic prevailed" in result.reason
    assert "FINAL round" in solver.prompts[-1]


# --- failures and control -------------------------------------------------


async def test_a_dead_solver_fails_the_run_cleanly(tmp_path):
    d, solver, critic = debate(tmp_path, ["claude not found in PATH"], [approve()])

    result = await d.run()

    assert result.outcome is Outcome.FAILED
    assert critic.prompts == [], "the critic should never have been asked"


async def test_a_malformed_critique_is_retried_once(tmp_path):
    d, solver, critic = debate(
        tmp_path, [solved()], [{"nonsense": True}, approve()]
    )

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS
    assert len(critic.prompts) == 2
    assert "did not match the required format" in critic.prompts[1]


async def test_a_critique_that_never_parses_fails_the_run(tmp_path):
    d, solver, critic = debate(
        tmp_path, [solved()], [{"nonsense": True}, {"still": "wrong"}]
    )

    result = await d.run()

    assert result.outcome is Outcome.FAILED


async def test_a_solver_reply_that_is_not_json_keeps_the_argument_alive(tmp_path):
    """The edits may well have landed even if the report came back malformed."""

    class Prose(Scripted):
        async def run(self, prompt, **kwargs):
            self.prompts.append(prompt)
            return TurnResult(text="I filled in the table.", structured=None, cost_usd=0.01)

    solver = Prose("solver", [])
    critic = Scripted("critic", [approve()])
    d = Debate(
        task="t", cwd=tmp_path, solver=solver, critic=critic,
        budget=Budget(Limits(max_usd=None, max_wall_seconds=None)),
    )

    result = await d.run()

    assert result.outcome is Outcome.CONSENSUS
    assert "not valid JSON" in result.rounds[0].notes[0]


async def test_stop_aborts_before_the_next_round(tmp_path):
    d, solver, critic = debate(
        tmp_path,
        [solved(), solved()],
        [changes("keep going"), approve()],
    )

    original = d.referee.judge

    def judge_then_stop(*args, **kwargs):
        d.stop()
        return original(*args, **kwargs)

    d.referee.judge = judge_then_stop
    result = await d.run()

    assert result.outcome is Outcome.ABORTED


async def test_round_hook_fires_once_per_round_not_per_turn(tmp_path):
    """Snapshots hang off this hook; firing it per turn would spam refs."""

    seen = []

    d, solver, critic = debate(
        tmp_path,
        [solved(), solved()],
        [changes("more"), approve()],
        on_round_start=lambda n: _record(seen, n),
    )

    await d.run()

    assert seen == [1, 2]


async def _record(sink, value):
    sink.append(value)


async def test_events_narrate_the_argument(tmp_path):
    events = []
    d, solver, critic = debate(
        tmp_path, [solved()], [approve()], on_event=events.append
    )

    await d.run()

    kinds = [e.kind for e in events]
    assert kinds.count("turn_start") == 2
    assert "verdict" in kinds
    assert kinds[-1] == "finished"
    roles = [e.role for e in events if e.kind == "turn_start"]
    assert roles == [Role.SOLVE, Role.CRITIQUE]
