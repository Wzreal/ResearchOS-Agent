import pytest

from researchos.application.budget_ledger import release_unstarted, reserve, settle
from researchos.application.errors import RuntimePreconditionError
from researchos.domain.runtime import (
    RuntimeBudgetState,
    RuntimeResourceAmount,
    UsageCertainty,
)


def budget() -> RuntimeBudgetState:
    return RuntimeBudgetState(
        limits=RuntimeResourceAmount(
            duration_milliseconds=100, tokens=100, cost_microunits=100, tool_calls=10
        )
    )


def test_exact_usage_commits_actual_and_releases_unused() -> None:
    reservation = RuntimeResourceAmount(
        duration_milliseconds=50, tokens=20, cost_microunits=10, tool_calls=2
    )
    active = reserve(budget(), reservation)
    settled = settle(
        active,
        reservation,
        usage=RuntimeResourceAmount(
            duration_milliseconds=30,
            tokens=10,
            cost_microunits=4,
            tool_calls=1,
        ),
        certainty=UsageCertainty.EXACT,
    )

    assert settled.reserved == RuntimeResourceAmount()
    assert settled.consumed.tokens == 10
    assert settled.available().tokens == 90


def test_unknown_usage_becomes_uncertain_and_is_not_released() -> None:
    reservation = RuntimeResourceAmount(duration_milliseconds=50, tokens=20)
    settled = settle(
        reserve(budget(), reservation),
        reservation,
        usage=None,
        certainty=UsageCertainty.UNKNOWN,
    )

    assert settled.reserved == RuntimeResourceAmount()
    assert settled.uncertain_consumption == reservation
    assert settled.available().tokens == 80


def test_upper_bound_is_charged_conservatively() -> None:
    reservation = RuntimeResourceAmount(duration_milliseconds=50, tokens=20)
    upper = RuntimeResourceAmount(duration_milliseconds=40, tokens=15)
    settled = settle(
        reserve(budget(), reservation),
        reservation,
        usage=upper,
        certainty=UsageCertainty.UPPER_BOUND,
    )
    assert settled.consumed == upper
    assert settled.available().tokens == 85


def test_overrun_is_recorded_honestly_and_breaches() -> None:
    reservation = RuntimeResourceAmount(duration_milliseconds=50, tokens=20)
    settled = settle(
        reserve(budget(), reservation),
        reservation,
        usage=RuntimeResourceAmount(duration_milliseconds=150, tokens=120),
        certainty=UsageCertainty.EXACT,
    )
    assert settled.consumed.tokens == 120
    assert settled.breached
    assert settled.available().tokens == -20


def test_usage_over_its_reservation_breaches_even_with_global_capacity() -> None:
    reservation = RuntimeResourceAmount(duration_milliseconds=10, tokens=10)
    settled = settle(
        reserve(budget(), reservation),
        reservation,
        usage=RuntimeResourceAmount(duration_milliseconds=11, tokens=11),
        certainty=UsageCertainty.EXACT,
    )
    assert settled.breached
    assert settled.available().tokens == 89


def test_full_release_is_only_explicit_unstarted_path() -> None:
    reservation = RuntimeResourceAmount(duration_milliseconds=50)
    assert release_unstarted(reserve(budget(), reservation), reservation) == budget()
    with pytest.raises(RuntimePreconditionError):
        reserve(budget(), RuntimeResourceAmount(tokens=101))
