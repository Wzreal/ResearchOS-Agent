"""State transitions for the separate claim-extraction dispatch journal."""

from researchos.application.errors import CorruptClaimExtractionOperation
from researchos.domain.claim_extraction import ClaimExtractionResponse
from researchos.domain.claim_extraction_operation import (
    ClaimExtractionOperation,
    ClaimExtractionOperationStatus,
)
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.interfaces.claim_extraction import ClaimExtractionOperationStore
from researchos.interfaces.lifecycle import Clock

_LEGAL: dict[
    ClaimExtractionOperationStatus, frozenset[ClaimExtractionOperationStatus]
] = {
    ClaimExtractionOperationStatus.PREPARED: frozenset(
        {
            ClaimExtractionOperationStatus.DISPATCHED,
            ClaimExtractionOperationStatus.FAILED,
            ClaimExtractionOperationStatus.CANCELLED,
        }
    ),
    ClaimExtractionOperationStatus.DISPATCHED: frozenset(
        {
            ClaimExtractionOperationStatus.VALIDATED,
            ClaimExtractionOperationStatus.FAILED,
            ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN,
        }
    ),
    ClaimExtractionOperationStatus.VALIDATED: frozenset(
        {
            ClaimExtractionOperationStatus.COMPLETED,
            ClaimExtractionOperationStatus.FAILED,
        }
    ),
    ClaimExtractionOperationStatus.COMPLETED: frozenset(),
    ClaimExtractionOperationStatus.FAILED: frozenset(),
    ClaimExtractionOperationStatus.CANCELLED: frozenset(),
    ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN: frozenset(),
}


class ClaimExtractionOperationManager:
    def __init__(self, *, store: ClaimExtractionOperationStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def create(self, operation: ClaimExtractionOperation) -> ClaimExtractionOperation:
        self._store.create(operation)
        return operation

    def transition(
        self,
        operation: ClaimExtractionOperation,
        status: ClaimExtractionOperationStatus,
        *,
        response: ClaimExtractionResponse | None = None,
        usage: RuntimeResourceAmount | None = None,
        usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN,
        mutation_keys: tuple[str, ...] | None = None,
        failure_code: str | None = None,
        provider_diagnostics: dict[str, object] | None = None,
    ) -> ClaimExtractionOperation:
        if status not in _LEGAL[operation.status]:
            raise CorruptClaimExtractionOperation("illegal claim extraction transition")
        updated = operation.model_copy(
            update={
                "checkpoint_revision": operation.checkpoint_revision + 1,
                "status": status,
                "validated_response": (
                    response if response is not None else operation.validated_response
                ),
                # Settlement is written with VALIDATED and must survive the
                # graph replay transition to COMPLETED.
                "usage": usage if usage is not None else operation.usage,
                "usage_certainty": (
                    usage_certainty
                    if usage is not None
                    or usage_certainty is not UsageCertainty.UNKNOWN
                    else operation.usage_certainty
                ),
                "mutation_keys": (
                    operation.mutation_keys
                    if mutation_keys is None
                    else mutation_keys
                ),
                "failure_code": failure_code,
                "provider_diagnostics": (
                    provider_diagnostics
                    if provider_diagnostics is not None
                    else operation.provider_diagnostics
                ),
                "updated_at": self._clock.now(),
            }
        )
        self._store.save(updated, expected_revision=operation.checkpoint_revision)
        return updated

    def recover(self, operation: ClaimExtractionOperation) -> ClaimExtractionOperation:
        if operation.status is ClaimExtractionOperationStatus.DISPATCHED:
            return self.transition(
                operation,
                ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN,
                failure_code="claim_extraction_interrupted_unknown",
            )
        return operation
