"""Spending limits for a run.

Claude reports what a turn actually cost. Codex reports only tokens, so its cost
is an estimate from configured prices — and zero prices mean "unknown", never
"free". Money limits are therefore enforced honestly for what is measurable and
backed by a token limit for what is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from dai.models import TurnResult, Usage


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float = 0.0
    cache_write_per_mtok: float = 0.0

    @property
    def known(self) -> bool:
        return any(
            (
                self.input_per_mtok,
                self.output_per_mtok,
                self.cache_read_per_mtok,
                self.cache_write_per_mtok,
            )
        )

    def estimate(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * self.cache_read_per_mtok
            + usage.cache_write_tokens * self.cache_write_per_mtok
        ) / 1_000_000


@dataclass(frozen=True)
class Limits:
    max_rounds: int = 5
    max_usd: float | None = 5.0
    max_tokens: int | None = None
    max_wall_seconds: float | None = 1800.0


@dataclass
class Spend:
    turns: int = 0
    measured_usd: float = 0.0
    estimated_usd: float = 0.0
    tokens: int = 0
    #: Engines that spent tokens with no price configured.
    unpriced: set[str] = field(default_factory=set)

    @property
    def usd(self) -> float:
        return self.measured_usd + self.estimated_usd

    @property
    def exact(self) -> bool:
        return not self.unpriced


class Budget:
    """Tracks spend and decides whether another turn may start."""

    def __init__(
        self,
        limits: Limits | None = None,
        pricing: dict[str, Pricing] | None = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self.limits = limits or Limits()
        self.pricing = pricing or {}
        self._clock = clock
        self._started = clock()
        self.spend = Spend()
        self.rounds = 0
        #: Rounds a human asked for after the two had agreed. They are played
        #: on top of the limit rather than out of it — being out of rounds is
        #: not a reason to refuse the person the rounds were spent for.
        self.extra_rounds = 0
        #: Time spent waiting on a person, which no agent was burning.
        self._held_since: float | None = None
        self._held = 0.0

    # --- accounting -------------------------------------------------------

    def record(self, engine: str, result: TurnResult) -> None:
        self.spend.turns += 1
        self.spend.tokens += result.usage.total

        if result.cost_usd is not None:
            self.spend.measured_usd += result.cost_usd
            return

        price = self.pricing.get(engine)
        if price is not None and price.known:
            self.spend.estimated_usd += price.estimate(result.usage)
        elif result.usage.total:
            # Remember that this engine's spend is invisible, so the UI can say
            # so rather than implying the total is complete.
            self.spend.unpriced.add(engine)

    @property
    def elapsed(self) -> float:
        held = self._held
        if self._held_since is not None:
            held += self._clock() - self._held_since
        return self._clock() - self._started - held

    def hold(self) -> None:
        """Stop the wall clock while a person decides something.

        The time limit is there to stop agents running away, not to charge
        someone for reading a diff: without this, ten minutes on the merge
        screen after a twenty-minute run would refuse them the extra round
        they were deciding to ask for.
        """

        if self._held_since is None:
            self._held_since = self._clock()

    def release(self) -> None:
        if self._held_since is not None:
            self._held += self._clock() - self._held_since
            self._held_since = None

    # --- gating -----------------------------------------------------------

    def resources_exhausted(self) -> str | None:
        """Money, tokens or time gone — anything a final enforced turn cannot ignore.

        Kept separate from the round limit: running out of rounds is a reason to
        stop arguing, but a deadlock resolution still needs one turn to apply
        the winning side's position.
        """

        limits = self.limits

        if limits.max_usd is not None and self.spend.usd >= limits.max_usd:
            return f"budget exhausted: ${self.spend.usd:.2f} of ${limits.max_usd:.2f}"

        if limits.max_tokens is not None and self.spend.tokens >= limits.max_tokens:
            return f"token budget exhausted: {self.spend.tokens:,} of {limits.max_tokens:,}"

        if limits.max_wall_seconds is not None and self.elapsed >= limits.max_wall_seconds:
            return f"time budget exhausted: {self.elapsed:.0f}s of {limits.max_wall_seconds:.0f}s"

        return None

    def stop_reason(self) -> str | None:
        """Why no further round may start, or None if there is room."""

        if (reason := self.resources_exhausted()) is not None:
            return reason

        counted = self.rounds - self.extra_rounds
        if counted >= self.limits.max_rounds:
            return f"round limit reached: {counted} of {self.limits.max_rounds}"

        return None

    def room_for_another_turn(self) -> str | None:
        """Stop *before* a turn we cannot afford, rather than mid-argument.

        Uses the average turn so far as the forecast. Cutting a round off
        halfway leaves the working tree in whatever state the solver reached,
        which is worse than stopping cleanly one round earlier.
        """

        return self.stop_reason() or self._forecast()

    def room_for_extra_round(self) -> str | None:
        """Why a round a person asked for cannot be afforded, if it cannot.

        The round limit does not apply — that is what makes it extra — but
        money, tokens and time do, and so does the forecast: an extra round cut
        off halfway leaves the same half-modified tree as any other.
        """

        return self.resources_exhausted() or self._forecast()

    def _forecast(self) -> str | None:
        limits = self.limits
        if limits.max_usd is not None and self.spend.turns:
            forecast = self.spend.usd + self.spend.usd / self.spend.turns
            if forecast > limits.max_usd:
                return (
                    f"stopping early: another turn would likely exceed "
                    f"${limits.max_usd:.2f} (spent ${self.spend.usd:.2f})"
                )
        return None
