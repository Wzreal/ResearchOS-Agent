"""In-memory CAS store for ClaimExtractionOperation."""

from threading import RLock

from researchos.application.errors import (
    ClaimExtractionOperationAlreadyExists,
    ClaimExtractionOperationNotFound,
    ClaimExtractionOperationRevisionConflict,
    CorruptClaimExtractionOperation,
)
from researchos.domain.claim_extraction_operation import ClaimExtractionOperation
from researchos.security.redaction import PersistenceRedactor


class InMemoryClaimExtractionOperationStore:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ClaimExtractionOperation] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, operation: ClaimExtractionOperation) -> None:
        self._validate(operation)
        with self._lock:
            key = (operation.run_id, operation.extraction_id)
            if key in self._items:
                raise ClaimExtractionOperationAlreadyExists(operation.extraction_id)
            if operation.checkpoint_revision != 0:
                raise ClaimExtractionOperationRevisionConflict(
                    "initial revision must be zero"
                )
            self._items[key] = operation.model_copy(deep=True)

    def load(self, run_id: str, extraction_id: str) -> ClaimExtractionOperation:
        with self._lock:
            try:
                item = self._items[(run_id, extraction_id)].model_copy(deep=True)
            except KeyError as exc:
                raise ClaimExtractionOperationNotFound(extraction_id) from exc
        self._validate(item)
        return item

    def save(
        self, operation: ClaimExtractionOperation, *, expected_revision: int
    ) -> None:
        self._validate(operation)
        with self._lock:
            key = (operation.run_id, operation.extraction_id)
            try:
                current = self._items[key]
            except KeyError as exc:
                raise ClaimExtractionOperationNotFound(operation.extraction_id) from exc
            if current.operation_id != operation.operation_id:
                raise ClaimExtractionOperationRevisionConflict(
                    "operation identity changed"
                )
            if current.checkpoint_revision != expected_revision:
                raise ClaimExtractionOperationRevisionConflict(
                    "operation revision differs"
                )
            if operation.checkpoint_revision != expected_revision + 1:
                raise ClaimExtractionOperationRevisionConflict(
                    "revision must increment once"
                )
            self._items[key] = operation.model_copy(deep=True)

    def _validate(self, operation: ClaimExtractionOperation) -> None:
        self._redactor.assert_safe_model(operation)
        try:
            validated = ClaimExtractionOperation.model_validate(
                operation.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptClaimExtractionOperation(
                "operation contract is invalid"
            ) from exc
        if validated != operation:
            raise CorruptClaimExtractionOperation("operation canonical form differs")
