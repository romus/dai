"""Spending limits, including the part where one engine's cost is invisible."""

from __future__ import annotations

from dai.budget import Budget, Limits, Pricing
from dai.models import TurnResult, Usage


def turn(cost=None, **usage):
    return TurnResult(cost_usd=cost, usage=Usage(**usage))


def test_measured_cost_is_used_when_the_engine_reports_it():
    budget = Budget()
    budget.record("claude", turn(cost=0.25, output_tokens=100))

    assert budget.spend.measured_usd == 0.25
    assert budget.spend.exact


def test_cost_is_estimated_from_pricing_when_absent():
    budget = Budget(pricing={"codex": Pricing(input_per_mtok=1.0, output_per_mtok=10.0)})
    budget.record("codex", turn(input_tokens=1_000_000, output_tokens=100_000))

    assert budget.spend.estimated_usd == 2.0
    assert budget.spend.exact


def test_unpriced_engines_are_flagged_rather_than_counted_as_free():
    """Zero configured price means unknown; reporting $0.00 would be a lie."""

    budget = Budget()
    budget.record("codex", turn(input_tokens=50_000, output_tokens=900))

    assert budget.spend.usd == 0.0
    assert not budget.spend.exact
    assert budget.spend.unpriced == {"codex"}


def test_a_turn_that_burned_nothing_does_not_flag_the_engine():
    budget = Budget()
    budget.record("codex", turn())

    assert budget.spend.exact


# --- gating ---------------------------------------------------------------


def test_money_limit_stops_the_run():
    budget = Budget(Limits(max_usd=1.0))
    budget.record("claude", turn(cost=1.5))

    assert "budget exhausted" in budget.stop_reason()


def test_token_limit_stops_the_run():
    budget = Budget(Limits(max_usd=None, max_tokens=1000))
    budget.record("codex", turn(input_tokens=1200))

    assert "token budget" in budget.stop_reason()


def test_round_limit_stops_the_run():
    budget = Budget(Limits(max_rounds=3, max_usd=None, max_wall_seconds=None))
    budget.rounds = 3

    assert "round limit" in budget.stop_reason()


def test_wall_clock_limit_stops_the_run():
    # The constructor reads the clock once to mark the start; every later read
    # is a measurement.
    reads = []

    def clock():
        reads.append(None)
        return 0.0 if len(reads) == 1 else 120.0

    budget = Budget(Limits(max_usd=None, max_wall_seconds=60), clock=clock)

    assert "time budget" in budget.stop_reason()


def test_nothing_spent_means_no_reason_to_stop():
    assert Budget().stop_reason() is None


def test_a_turn_we_cannot_afford_is_refused_before_it_starts():
    """Cutting a round off mid-flight leaves the tree half-edited."""

    budget = Budget(Limits(max_usd=1.0, max_wall_seconds=None))
    budget.record("claude", turn(cost=0.6))

    # Spent 0.60; the next turn is forecast at 0.60 too, so 1.20 > 1.00.
    assert budget.stop_reason() is None
    assert "would likely exceed" in budget.room_for_another_turn()


def test_room_remains_when_the_forecast_fits():
    budget = Budget(Limits(max_usd=10.0, max_wall_seconds=None))
    budget.record("claude", turn(cost=0.6))

    assert budget.room_for_another_turn() is None


def test_usage_totals_add_up():
    total = Usage(input_tokens=1, output_tokens=2) + Usage(input_tokens=3, cache_read_tokens=4)

    assert total.input_tokens == 4
    assert total.total == 10


# --- extra rounds ----------------------------------------------------------


def test_an_extra_round_is_not_counted_against_the_limit():
    """A round a person asked for after agreement is played on top of the limit."""

    budget = Budget(Limits(max_rounds=3, max_usd=None, max_wall_seconds=None))
    budget.rounds = 3
    assert "round limit reached: 3 of 3" in budget.stop_reason()

    budget.extra_rounds = 1
    budget.rounds = 4

    assert budget.stop_reason() == "round limit reached: 3 of 3"
    budget.rounds = 3
    assert budget.stop_reason() is None


def test_an_extra_round_still_has_to_be_affordable():
    budget = Budget(Limits(max_usd=1.0, max_rounds=1, max_wall_seconds=None))
    budget.rounds = 1
    budget.record("claude", turn(cost=0.6))

    # Out of rounds is not a reason; the money forecast is.
    assert "would likely exceed" in budget.room_for_extra_round()


def test_an_extra_round_ignores_the_round_limit_alone():
    budget = Budget(Limits(max_usd=None, max_rounds=1, max_wall_seconds=None))
    budget.rounds = 1

    assert budget.room_for_another_turn() is not None
    assert budget.room_for_extra_round() is None


def test_time_spent_waiting_on_a_person_is_not_charged():
    """Reading a diff for ten minutes must not use up the agents' time limit."""

    now = [0.0]
    budget = Budget(Limits(max_usd=None, max_wall_seconds=60), clock=lambda: now[0])

    now[0] = 30.0
    budget.hold()
    now[0] = 600.0

    assert budget.elapsed == 30.0
    assert budget.resources_exhausted() is None

    budget.release()
    now[0] = 610.0

    assert budget.elapsed == 40.0
    budget.release()  # releasing twice changes nothing
    assert budget.elapsed == 40.0
