"""The argument itself: solve, critique, rebut, repeat until it settles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from dai.budget import Budget, Spend
from dai.consensus import Assessment, Referee
from dai.engines import Engine, Session
from dai.models import (
    Access,
    AgentEvent,
    CriticTurn,
    Issue,
    Outcome,
    Role,
    SolverTurn,
    TurnResult,
)
from dai.protocol import (
    CRITIC_SCHEMA,
    SOLVER_SCHEMA,
    STANDARD,
    arbitrated_prompt,
    critique_first_prompt,
    critique_next_prompt,
    parse_critic,
    parse_solver,
    prove_prompt,
    rebut_prompt,
    solve_prompt,
)

#: What to do when the two will not converge.
DEADLOCK_POLICIES = ("critic", "solver", "ask")

#: How a human called each open issue, keyed by `Issue.fingerprint` rather than
#: by id: this outlives the critique it came from — it becomes the strike list
#: consulted every later round — and ids are per-critique, renumbered freely.
Ruling = dict[str, str]

SIDES = ("critic", "solver")


def as_ruling(answer: Ruling | str, issues: list[Issue]) -> Ruling:
    """Normalise an answer to a verdict per issue.

    A bare side is still a valid answer and means "this one wins everything" —
    that is what a headless run and an exhausted budget can offer. Anything
    unrecognised is dropped rather than guessed at, and a missing verdict counts
    as upheld: silence must never strike an issue out of the argument.
    """

    if isinstance(answer, str):
        side = answer if answer in SIDES else "critic"
        return {issue.fingerprint: side for issue in issues}
    return {key: side for key, side in (answer or {}).items() if side in SIDES}


@dataclass
class Round:
    number: int
    solver: SolverTurn | None = None
    critic: CriticTurn | None = None
    assessment: Assessment | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class DebateEvent:
    """Progress report for the UI."""

    kind: str  # round | turn_start | turn_end | verdict | note | finished
    round: int = 0
    role: Role | None = None
    engine: str = ""
    text: str = ""


@dataclass
class DebateResult:
    outcome: Outcome
    reason: str
    rounds: list[Round]
    spend: Spend
    open_issues: list[Issue] = field(default_factory=list)

    @property
    def agreed(self) -> bool:
        return self.outcome is Outcome.CONSENSUS


class Debate:
    """Runs one task through the solver/critic loop."""

    def __init__(
        self,
        *,
        task: str,
        cwd: Path,
        solver: Engine,
        critic: Engine,
        budget: Budget,
        referee: Referee | None = None,
        deadlock_policy: str = "critic",
        language: str = "auto",
        rigor: str = STANDARD,
        solver_writes: bool = True,
        on_event: Callable[[DebateEvent], None] | None = None,
        on_agent_event: Callable[[Role, AgentEvent], None] | None = None,
        on_round_start: Callable[[int], Awaitable[None]] | None = None,
        on_deadlock: Callable[[DebateResult], Awaitable[Ruling | str]] | None = None,
        turn_timeout: float | None = None,
    ) -> None:
        self.task = task
        self.cwd = cwd
        self.solver = solver
        self.critic = critic
        self.budget = budget
        self.referee = referee or Referee()
        self.deadlock_policy = deadlock_policy
        self.language = language
        self.rigor = rigor
        self.solver_access = Access.WRITE if solver_writes else Access.READ_ONLY
        self.on_event = on_event
        self.on_agent_event = on_agent_event
        self.on_round_start = on_round_start
        self.on_deadlock = on_deadlock
        self.turn_timeout = turn_timeout

        self.rounds: list[Round] = []
        self._solver_session = Session()
        self._critic_session = Session()
        self._paused = asyncio.Event()
        self._paused.set()
        self._stopped = False
        self._announced: set[int] = set()
        self._injections: list[str] = []
        #: Rounds where the critic has already been sent its verdict back once.
        self._proved: set[int] = set()
        #: Complaints a human dismissed at a deadlock. Struck on sight for the
        #: rest of the run — without this the same argument simply comes back.
        self._struck: dict[str, Issue] = {}
        #: Issues a human upheld, owed to the solver as binding instructions for
        #: exactly one turn.
        self._upheld: list[Issue] = []
        #: Rounds whose complaints were *all* struck. Such a round must not read
        #: as agreement: consensus is what opens the merge dialog, and nobody
        #: approved anything here.
        self._emptied: set[int] = set()
        #: Whether the softer of the referee's two approval rules has been spent.
        self._challenged = False

    # --- control ----------------------------------------------------------

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def stop(self) -> None:
        self._stopped = True
        self._paused.set()

    def inject(self, message: str) -> None:
        """Add your own voice to the argument.

        Delivered at the start of the next turn, to whichever agent moves next.
        Two agents can be confidently wrong together; this is the seat at the
        table for the person who knows what the task actually meant.
        """

        if message.strip():
            self._injections.append(message.strip())

    def abort_result(self, reason: str) -> DebateResult:
        """The ledger for a run cut down from outside, mid-turn.

        The TUI's kill switch cancels the running turn outright, so the loop
        never reaches its own ABORTED exit; the frontend asks here for the
        rounds and spend so far to record before quitting. Whatever the killed
        half-turn consumed was never reported back, so it is absent from the
        spend. Call only once the run's task is fully finished.
        """

        return self._finish(Outcome.ABORTED, reason)

    @property
    def last_verdict(self) -> str:
        """How the referee called the most recently judged round, in one line.

        For whoever is recording the run — a commit message wants to say what
        the round it holds actually settled. Empty before the first assessment.
        """

        for rnd in reversed(self.rounds):
            if rnd.assessment is not None:
                return f"round {rnd.number}: {rnd.assessment.reason}"
        return ""

    # --- main loop --------------------------------------------------------

    async def run(self) -> DebateResult:
        current = Round(number=1)
        self.rounds.append(current)
        await self._gate(current.number)

        solver_turn = await self._solve(current)
        if solver_turn is None:
            return self._finish(Outcome.FAILED, "the solver produced no usable result")

        previous_critique: CriticTurn | None = None

        while True:
            if self._stopped:
                return self._finish(Outcome.ABORTED, "stopped by the user")

            critique = await self._critique(current, previous_critique, solver_turn)
            if critique is None:
                return self._finish(Outcome.FAILED, "the critic produced no usable verdict")
            emptied = current.number in self._emptied

            assessment = self.referee.judge(
                critique, previous=previous_critique, solver=solver_turn
            )
            current.assessment = assessment
            self._note_referee(current, assessment)
            self._emit(
                DebateEvent(
                    kind="verdict",
                    round=current.number,
                    engine=self.critic.name,
                    text=f"{critique.verdict.value}: {assessment.reason}",
                )
            )

            # Striking may not manufacture an agreement. An emptied open set
            # under REQUEST_CHANGES settles the referee, and consensus is what
            # opens the merge dialog — over work this critic never approved.
            # Only a set that was already empty may end the argument.
            if assessment.settled and not emptied:
                return self._finish(Outcome.CONSENSUS, assessment.reason)

            if assessment.deadlocked and not emptied:
                if (ending := await self._resolve_deadlock(current, critique)) is not None:
                    return ending
                # Ruled issue by issue: `critique` now holds only what was
                # upheld, and the loop tail below sends it to the solver as it
                # sends any other round's open issues.

            if not critique.open_issues:
                # An approval the critic would not substantiate, even after being
                # asked. There is nothing to rebut, and sending a write-access
                # agent to "make the fixes" over an empty list is how this used
                # to end. No policy applies either: there is nothing to enforce.
                return self._finish(
                    Outcome.DEADLOCK, "the critic would not substantiate its approval"
                )

            # Count this round as spent *before* asking whether another fits,
            # otherwise the limit is checked one round behind and max_rounds=5
            # runs six.
            self.budget.rounds = current.number
            if (reason := self.budget.room_for_another_turn()) is not None:
                outcome = (
                    Outcome.ROUNDS if "round limit" in reason else Outcome.BUDGET
                )
                return await self._wrap_up(outcome, reason, current, critique)

            # Another round: the solver answers, then the critic re-checks.
            previous_critique = critique
            next_round = Round(number=current.number + 1)
            self.rounds.append(next_round)
            await self._gate(next_round.number)
            if self._stopped:
                return self._finish(Outcome.ABORTED, "stopped by the user")

            solver_turn = await self._rebut(next_round, critique.open_issues)
            if solver_turn is None:
                return self._finish(Outcome.FAILED, "the solver stopped responding")
            current = next_round

    # --- turns ------------------------------------------------------------

    async def _solve(self, rnd: Round) -> SolverTurn | None:
        result = await self._turn(
            Role.SOLVE,
            self.solver,
            self._solver_session,
            solve_prompt(self.task, language=self.language, rigor=self.rigor),
            access=self.solver_access,
            schema=SOLVER_SCHEMA,
            round_no=rnd.number,
        )
        rnd.solver = parse_solver(result.structured) if result else None
        if rnd.solver is None and result is not None and result.ok:
            # The work may well be done even if the report came back malformed.
            rnd.solver = SolverTurn(summary=result.text[:2000])
            rnd.notes.append("solver report was not valid JSON; using its raw text")
        return rnd.solver

    async def _rebut(self, rnd: Round, issues: list[Issue]) -> SolverTurn | None:
        final = self._is_final_round(rnd.number)
        if self._upheld:
            # A person ruled on these, so this turn answers them rather than the
            # critic. Binding for exactly one turn: the argument resumes after.
            prompt = arbitrated_prompt(
                self._upheld,
                list(self._struck.values()),
                language=self.language,
                rigor=self.rigor,
            )
            self._upheld = []
        else:
            prompt = rebut_prompt(
                issues, final=final, language=self.language, rigor=self.rigor
            )
        result = await self._turn(
            Role.REBUT,
            self.solver,
            self._solver_session,
            prompt,
            access=self.solver_access,
            schema=SOLVER_SCHEMA,
            round_no=rnd.number,
        )
        rnd.solver = parse_solver(result.structured) if result else None
        if rnd.solver is None and result is not None and result.ok:
            rnd.solver = SolverTurn(summary=result.text[:2000])
            rnd.notes.append("solver reply was not valid JSON; using its raw text")
        return rnd.solver

    async def _critique(
        self,
        rnd: Round,
        previous: CriticTurn | None,
        solver: SolverTurn,
    ) -> CriticTurn | None:
        if previous is None:
            prompt = critique_first_prompt(
                self.task, solver, language=self.language, rigor=self.rigor
            )
        else:
            # Open issues only. The critic is told to keep the ones it conceded
            # in `issues`, and a conceded issue rendered back at it comes with
            # "NO ANSWER — the solver ignored this one" against a point the
            # critic itself dropped: it re-raises, the open set churns, and the
            # referee never sees the argument stall.
            prompt = critique_next_prompt(
                rnd.number,
                previous.open_issues,
                solver,
                language=self.language,
                rigor=self.rigor,
            )

        result = await self._turn(
            Role.CRITIQUE,
            self.critic,
            self._critic_session,
            prompt,
            access=Access.READ_ONLY,
            schema=CRITIC_SCHEMA,
            round_no=rnd.number,
        )
        if result is None:
            return None

        critique = parse_critic(result.structured)
        if critique is None and result.ok:
            # One retry: a critique without a parseable verdict is not something
            # we may guess at, but it is usually a formatting slip.
            rnd.notes.append("critic returned no valid verdict; asking again")
            retry = await self._turn(
                Role.CRITIQUE,
                self.critic,
                self._critic_session,
                "Your previous reply did not match the required format. "
                "Reply again with the verdict object only.",
                access=Access.READ_ONLY,
                schema=CRITIC_SCHEMA,
                round_no=rnd.number,
            )
            critique = parse_critic(retry.structured) if retry else None

        if critique is not None:
            critique = await self._prove(rnd, critique, solver)
            self._strike(rnd, critique)

        rnd.critic = critique
        return critique

    def _strike(self, rnd: Round, critique: CriticTurn) -> bool:
        """Drop what a human already dismissed, and say whether any were.

        Runs before anyone judges, deliberately: `Referee._track_progress`
        fingerprints `open_issues`, so a set filtered after judging would have
        every round look stalled against a complaint that no longer counts.
        """

        if not self._struck or not critique.issues:
            return False
        kept = [i for i in critique.issues if i.fingerprint not in self._struck]
        dropped = [i for i in critique.issues if i.fingerprint in self._struck]
        if dropped and not kept:
            self._emptied.add(rnd.number)
        for issue in dropped:
            note = f"[{issue.id}] struck — you dismissed this at the deadlock"
            rnd.notes.append(note)
            self._emit(
                DebateEvent(
                    kind="note", round=rnd.number, engine=self.critic.name, text=note
                )
            )
        critique.issues = kept
        return bool(dropped)

    async def _prove(
        self, rnd: Round, critique: CriticTurn, solver: SolverTurn
    ) -> CriticTurn:
        """Send an approval nobody can audit back to the critic. Once per round.

        The referee catches it either way, but the finding used to land on the
        wrong agent: with no open issues to rebut, the next turn was the solver's
        — write access, an empty issue list, and "make the fixes in the
        repository now". This spends the turn on the side that owes the work,
        and it is what makes the prompt's "you will be asked again" true.
        """

        why = self.referee.audit(critique, solver)
        if why is None or rnd.number in self._proved:
            return critique
        if critique.checked:
            # It said *something*, so this is the soft rule: its prose named
            # nothing the solver changed. Worth one challenge a run, then it
            # stands down — a critic that reviews by symbol name must not be
            # made to pay a turn for it every round.
            if self._challenged:
                return critique
            self._challenged = True

        self._proved.add(rnd.number)
        rnd.notes.append(f"{why}; asked it to show its work")
        self._emit(
            DebateEvent(
                kind="note",
                round=rnd.number,
                engine=self.critic.name,
                text=f"{why}; asking it to show its work",
            )
        )
        again = await self._turn(
            Role.CRITIQUE,
            self.critic,
            self._critic_session,
            prove_prompt(why, language=self.language),
            access=Access.READ_ONLY,
            schema=CRITIC_SCHEMA,
            round_no=rnd.number,
        )
        reasked = parse_critic(again.structured) if again else None
        return reasked if reasked is not None else critique

    async def _turn(
        self,
        role: Role,
        engine: Engine,
        session: Session,
        prompt: str,
        *,
        access: Access,
        schema: dict,
        round_no: int,
    ) -> TurnResult | None:
        await self._gate(round_no)
        if self._stopped:
            return None

        self._emit(
            DebateEvent(kind="turn_start", round=round_no, role=role, engine=engine.name)
        )

        if self._injections:
            # The user's word carries more weight than either agent's: say so,
            # or it reads as just another opinion to weigh up.
            notes = "\n".join(f"- {line}" for line in self._injections)
            prompt = (
                "The human running this session has intervened. Their instruction "
                f"overrides your own judgement and the other agent's:\n{notes}\n\n{prompt}"
            )
            self._injections.clear()

        sink = None
        if self.on_agent_event is not None:
            sink = lambda event, r=role: self.on_agent_event(r, event)  # noqa: E731

        result = await engine.run(
            prompt,
            cwd=self.cwd,
            access=access,
            schema=schema,
            session=session,
            on_event=sink,
            timeout=self.turn_timeout,
        )
        self.budget.record(engine.name, result)

        self._emit(
            DebateEvent(
                kind="turn_end",
                round=round_no,
                role=role,
                engine=engine.name,
                text=result.error or "",
            )
        )
        if not result.ok:
            self._emit(
                DebateEvent(
                    kind="note",
                    round=round_no,
                    engine=engine.name,
                    text=f"{engine.name} failed: {result.error}",
                )
            )
        return result

    # --- endings ----------------------------------------------------------

    async def _resolve_deadlock(
        self, rnd: Round, critique: CriticTurn
    ) -> DebateResult | None:
        """Both sides are repeating themselves. Ask, then carry on.

        Returns the run's ending, or `None` meaning the argument continues —
        which is the whole point of asking a person. A ruling is not a verdict
        on the run, it is evidence the run did not have: the issues upheld go
        back to the solver as binding instructions, the dismissed ones leave the
        argument for good, and the critic reviews what comes back. A run that
        was going to end in disagreement can now end in agreement, which is also
        the only way it is ever offered a merge.
        """

        pending = self._finish(
            Outcome.DEADLOCK,
            "neither side moved for "
            f"{self.referee.no_progress_rounds} round(s)",
            critique,
        )

        if self.deadlock_policy != "ask" or self.on_deadlock is None:
            return await self._apply_policy(self.deadlock_policy, pending, rnd, critique)

        ruling = as_ruling(await self.on_deadlock(pending), critique.open_issues)
        dismissed = [
            issue
            for issue in critique.open_issues
            if ruling.get(issue.fingerprint) == "solver"
        ]
        for issue in dismissed:
            self._struck[issue.fingerprint] = issue
        self._strike(rnd, critique)
        upheld = list(critique.open_issues)

        if not upheld:
            return self._finish(
                Outcome.CONSENSUS,
                "you dismissed every open issue; the solver's work stands",
            )

        self._upheld = upheld
        # A stall is a fact about rounds in which nobody moved, and this moved.
        # Without forgetting it the very next round is still stale and lands
        # straight back here.
        self.referee.forget_progress()
        self._emit(
            DebateEvent(
                kind="note",
                round=rnd.number,
                text=(
                    f"you upheld {len(upheld)} of {len(upheld) + len(dismissed)}; "
                    "the solver must apply them"
                ),
            )
        )
        return None

    async def _wrap_up(
        self, outcome: Outcome, reason: str, rnd: Round, critique: CriticTurn
    ) -> DebateResult:
        """Out of rounds or money: let the configured policy have the last word."""

        pending = self._finish(outcome, reason, critique)
        policy = self.deadlock_policy
        if policy == "ask" and self.on_deadlock is not None:
            # No rounds or money left, so there is nothing to continue into: a
            # per-issue ruling collapses to "is anything left to apply?".
            ruling = as_ruling(await self.on_deadlock(pending), critique.open_issues)
            critique.issues = [
                issue
                for issue in critique.open_issues
                if ruling.get(issue.fingerprint) != "solver"
            ]
            policy = "critic" if critique.issues else "solver"
        return await self._apply_policy(policy, pending, rnd, critique)

    async def _apply_policy(
        self, policy: str, pending: DebateResult, rnd: Round, critique: CriticTurn
    ) -> DebateResult:
        if policy == "solver" or not critique.open_issues:
            pending.reason += " — solver's version stands"
            return pending

        if policy != "critic":
            return pending  # 'ask' with nobody to ask, or 'stop'

        # Deliberately ignores the round limit: being out of rounds is exactly
        # how we got here. Only real resource exhaustion can block the last turn.
        if (blocked := self.budget.resources_exhausted()) is not None:
            pending.reason += f" — critic's objections stand, but {blocked}"
            return pending

        # One last enforced pass: the critic's remaining points get implemented
        # without further argument.
        final = Round(number=rnd.number + 1)
        self.rounds.append(final)
        self._emit(
            DebateEvent(
                kind="note",
                round=final.number,
                text="deadlock resolved in the critic's favour; applying its objections",
            )
        )
        solver_turn = await self._turn(
            Role.REBUT,
            self.solver,
            self._solver_session,
            rebut_prompt(critique.open_issues, final=True, language=self.language),
            access=self.solver_access,
            schema=SOLVER_SCHEMA,
            round_no=final.number,
        )
        final.solver = parse_solver(solver_turn.structured) if solver_turn else None
        pending.rounds = self.rounds
        pending.reason += " — critic prevailed; its objections were applied"
        return pending

    def _finish(
        self, outcome: Outcome, reason: str, critique: CriticTurn | None = None
    ) -> DebateResult:
        self.budget.rounds = len(self.rounds)
        result = DebateResult(
            outcome=outcome,
            reason=reason,
            rounds=self.rounds,
            spend=self.budget.spend,
            open_issues=list(critique.open_issues) if critique else [],
        )
        self._emit(
            DebateEvent(kind="finished", round=len(self.rounds), text=f"{outcome.value}: {reason}")
        )
        return result

    # --- helpers ----------------------------------------------------------

    def _is_final_round(self, number: int) -> bool:
        return number >= self.budget.limits.max_rounds

    def _note_referee(self, rnd: Round, assessment: Assessment) -> None:
        if assessment.rubber_stamp:
            rnd.notes.append(assessment.reason)
        for issue in assessment.repeats:
            rnd.notes.append(
                f"critic re-raised [{issue.id}] without answering the rebuttal"
            )

    async def _gate(self, round_no: int) -> None:
        """Honour pause, and let the host snapshot once per round.

        Every turn passes through here, so the round hook is fired only the
        first time a given round is seen — otherwise a snapshot would be taken
        before each of the three turns in a round.
        """

        await self._paused.wait()
        if self.on_round_start is not None and round_no not in self._announced:
            self._announced.add(round_no)
            await self.on_round_start(round_no)

    def _emit(self, event: DebateEvent) -> None:
        if self.on_event is not None:
            self.on_event(event)
