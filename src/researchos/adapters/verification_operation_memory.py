"""In-memory Phase 8 verification-operation CAS store."""

from __future__ import annotations

from threading import RLock

from researchos.application.errors import (
    CorruptVerificationOperation,
    VerificationOperationAlreadyExists,
    VerificationOperationNotFound,
    VerificationOperationRevisionConflict,
)
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.verification_runtime import (
    DEFAULT_MAX_OPERATION_CHECKPOINT_BYTES,
    MAX_OPERATION_CHECKPOINT_BYTES,
    VerificationOperation,
    VerificationOperationEnvelope,
    VerificationOperationStatus,
)
from researchos.security.redaction import PersistenceRedactor


class InMemoryVerificationOperationStore:
    def __init__(
        self, *, max_checkpoint_bytes: int = DEFAULT_MAX_OPERATION_CHECKPOINT_BYTES
    ) -> None:
        if not 1 <= max_checkpoint_bytes <= MAX_OPERATION_CHECKPOINT_BYTES:
            raise ValueError("operation checkpoint byte limit is invalid")
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._operations: dict[tuple[str, str], VerificationOperation] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, operation: VerificationOperation) -> None:
        self._validate(operation)
        self._validate_size(operation)
        with self._lock:
            key = (operation.run_id, operation.verification_id)
            if key in self._operations:
                raise VerificationOperationAlreadyExists(operation.run_id)
            self._assert_generation_create_allowed(
                operation,
                tuple(
                    value
                    for (stored_run_id, _), value in self._operations.items()
                    if stored_run_id == operation.run_id
                ),
            )
            if operation.checkpoint_revision != 0:
                raise VerificationOperationRevisionConflict(
                    "initial operation revision must be zero"
                )
            self._operations[key] = operation.model_copy(deep=True)

    @staticmethod
    def _assert_generation_create_allowed(
        operation: VerificationOperation,
        existing: tuple[VerificationOperation, ...],
    ) -> None:
        if not existing:
            return
        if operation.expected_prior_verification_id is None or any(
            item.status is not VerificationOperationStatus.COMPLETED
            for item in existing
        ):
            raise VerificationOperationAlreadyExists(
                "another verification generation is unresolved"
            )
        if operation.expected_prior_verification_id not in {
            item.verification_id for item in existing
        }:
            raise VerificationOperationAlreadyExists(
                "completed generations do not include declared predecessor"
            )

    def load(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        with self._lock:
            if verification_id is None:
                matches = [
                    value
                    for (stored_run_id, _), value in self._operations.items()
                    if stored_run_id == run_id
                ]
                if len(matches) != 1:
                    raise VerificationOperationNotFound(run_id)
                operation = matches[0].model_copy(deep=True)
            else:
                try:
                    operation = self._operations[
                        (run_id, verification_id)
                    ].model_copy(deep=True)
                except KeyError as exc:
                    raise VerificationOperationNotFound(run_id) from exc
            self._validate(operation)
            self._validate_size(operation)
            return operation

    def list_for_run(self, run_id: str) -> tuple[VerificationOperation, ...]:
        with self._lock:
            operations = tuple(
                value.model_copy(deep=True)
                for (stored_run_id, _), value in sorted(self._operations.items())
                if stored_run_id == run_id
            )
            for operation in operations:
                self._validate(operation)
                self._validate_size(operation)
            return operations

    def save(
        self, operation: VerificationOperation, *, expected_revision: int
    ) -> None:
        self._validate(operation)
        self._validate_size(operation)
        with self._lock:
            key = (operation.run_id, operation.verification_id)
            try:
                current = self._operations[key]
            except KeyError as exc:
                raise VerificationOperationNotFound(operation.run_id) from exc
            if current.operation_id != operation.operation_id:
                raise VerificationOperationRevisionConflict(
                    "verification operation identity changed"
                )
            if current.checkpoint_revision != expected_revision:
                raise VerificationOperationRevisionConflict(
                    f"expected revision {expected_revision}, "
                    f"found {current.checkpoint_revision}"
                )
            if operation.checkpoint_revision != expected_revision + 1:
                raise VerificationOperationRevisionConflict(
                    "operation revision must increase by exactly one"
                )
            self._operations[key] = operation.model_copy(deep=True)

    def _validate(self, operation: VerificationOperation) -> None:
        self._redactor.assert_safe_model(operation)
        try:
            validated = VerificationOperation.model_validate(
                operation.model_dump(mode="python")
            )
        except ValueError as exc:
            raise CorruptVerificationOperation(
                "verification operation contract is invalid"
            ) from exc
        if validated != operation:
            raise CorruptVerificationOperation(
                "verification operation canonical form differs"
            )

    def _validate_size(self, operation: VerificationOperation) -> None:
        envelope = VerificationOperationEnvelope(
            operation=operation, payload_sha256=model_sha256(operation)
        )
        if len(canonical_json_bytes(envelope) + b"\n") > self._max_checkpoint_bytes:
            raise CorruptVerificationOperation(
                "verification operation exceeds checkpoint byte limit"
            )
