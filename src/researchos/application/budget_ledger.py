"""Pure immutable reservation and settlement operations."""

from __future__ import annotations

from researchos.application.errors import RuntimePreconditionError
from researchos.domain.runtime import (
    RuntimeBudgetState,
    RuntimeResourceAmount,
    UsageCertainty,
)


def reserve(
    budget: RuntimeBudgetState, amount: RuntimeResourceAmount
) -> RuntimeBudgetState:
    if budget.breached or not amount.fits_within(budget.available()):
        raise RuntimePreconditionError("runtime budget cannot satisfy reservation")
    return budget.model_copy(update={"reserved": budget.reserved.plus(amount)})


def release_unstarted(
    budget: RuntimeBudgetState, reservation: RuntimeResourceAmount
) -> RuntimeBudgetState:
    """Full release is legal only when dispatch is proven not to have occurred."""

    return budget.model_copy(update={"reserved": budget.reserved.minus(reservation)})


def settle(
    budget: RuntimeBudgetState,
    reservation: RuntimeResourceAmount,
    *,
    usage: RuntimeResourceAmount | None,
    certainty: UsageCertainty,
) -> RuntimeBudgetState:
    remaining_reserved = budget.reserved.minus(reservation)
    if certainty is UsageCertainty.UNKNOWN:
        if usage is not None:
            raise RuntimePreconditionError("unknown usage must not contain an amount")
        # An attempted operation may still be running or may have produced
        # unreported side effects. Its full authorization remains charged as
        # uncertain until a future provider-specific reconciliation exists.
        uncertain = budget.uncertain_consumption.plus(reservation)
        return budget.model_copy(
            update={"reserved": remaining_reserved, "uncertain_consumption": uncertain}
        )
    if usage is None:
        raise RuntimePreconditionError("known usage certainty requires an amount")
    # EXACT is measured consumption. UPPER_BOUND is deliberately charged at
    # the reported upper bound; both safely release the unused reservation.
    consumed = budget.consumed.plus(usage)
    accounted = consumed.plus(budget.uncertain_consumption).plus(
        remaining_reserved
    )
    breached = (
        budget.breached
        or not usage.fits_within(reservation)
        or not accounted.fits_within(budget.limits)
    )
    return budget.model_copy(
        update={
            "reserved": remaining_reserved,
            "consumed": consumed,
            "breached": breached,
        }
    )
