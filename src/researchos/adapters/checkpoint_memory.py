"""Deterministic in-memory checkpoint store with compare-and-swap revisions."""

from threading import RLock

from researchos.application.errors import (
    CheckpointAlreadyExists,
    CheckpointNotFound,
    CheckpointRevisionConflict,
    CorruptCheckpoint,
)
from researchos.domain.contracts import model_sha256
from researchos.domain.runtime import RuntimeCheckpoint
from researchos.security.redaction import PersistenceRedactor


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._items: dict[str, RuntimeCheckpoint] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, checkpoint: RuntimeCheckpoint) -> None:
        self._validate(checkpoint)
        with self._lock:
            if checkpoint.run_id in self._items:
                raise CheckpointAlreadyExists(checkpoint.run_id)
            self._items[checkpoint.run_id] = checkpoint.model_copy(deep=True)

    def load(self, run_id: str) -> RuntimeCheckpoint:
        with self._lock:
            try:
                checkpoint = self._items[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise CheckpointNotFound(run_id) from exc
        self._validate(checkpoint)
        if checkpoint.run_id != run_id:
            raise CorruptCheckpoint("checkpoint run identity differs")
        return checkpoint

    def save(self, checkpoint: RuntimeCheckpoint, *, expected_revision: int) -> None:
        self._validate(checkpoint)
        with self._lock:
            try:
                current = self._items[checkpoint.run_id]
            except KeyError as exc:
                raise CheckpointNotFound(checkpoint.run_id) from exc
            if current.checkpoint_revision != expected_revision:
                raise CheckpointRevisionConflict(
                    f"expected checkpoint revision {expected_revision}, "
                    f"found {current.checkpoint_revision}"
                )
            if checkpoint.checkpoint_revision != expected_revision + 1:
                raise CheckpointRevisionConflict(
                    "checkpoint revision must increase by exactly one"
                )
            self._items[checkpoint.run_id] = checkpoint.model_copy(deep=True)

    def _validate(self, checkpoint: RuntimeCheckpoint) -> None:
        self._redactor.assert_safe_model(checkpoint)
        if checkpoint.dag_hash != model_sha256(checkpoint.dag):
            raise CorruptCheckpoint("checkpoint DAG hash differs")
        if checkpoint.execution_policy_hash != model_sha256(
            checkpoint.execution_policy
        ):
            raise CorruptCheckpoint("checkpoint policy hash differs")
