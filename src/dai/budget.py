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
        return self._clock() - self._started

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

        if self.rounds >= self.limits.max_rounds:
            return f"round limit reached: {self.rounds} of {self.limits.max_rounds}"

        return None

    def room_for_another_turn(self) -> str | None:
        """Stop *before* a turn we cannot afford, rather than mid-argument.

        Uses the average turn so far as the forecast. Cutting a round off
        halfway leaves the working tree in whatever state the solver reached,
        which is worse than stopping cleanly one round earlier.
        """

        if (reason := self.stop_reason()) is not None:
            return reason

        limits = self.limits
        if limits.max_usd is not None and self.spend.turns:
            forecast = self.spend.usd + self.spend.usd / self.spend.turns
            if forecast > limits.max_usd:
                return (
                    f"stopping early: another turn would likely exceed "
                    f"${limits.max_usd:.2f} (spent ${self.spend.usd:.2f})"
                )
        return None
