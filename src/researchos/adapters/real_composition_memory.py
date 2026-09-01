"""In-memory immutable REAL composition authority."""

from __future__ import annotations

from threading import RLock

from researchos.application.errors import (
    RealCompositionConflict,
    RealCompositionCorruption,
    RealCompositionNotFound,
    UnsafePersistenceData,
)
from researchos.domain.real_composition import (
    RealCompositionEnvelope,
    RealCompositionSnapshot,
)
from researchos.interfaces.lifecycle import Clock
from researchos.security.redaction import PersistenceRedactor


class InMemoryRealCompositionStore:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._items: dict[str, RealCompositionEnvelope] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: RealCompositionSnapshot) -> RealCompositionEnvelope:
        self._validate_snapshot(snapshot)
        run_id = snapshot.semantic.run_id
        with self._lock:
            existing = self._items.get(run_id)
            if existing is not None:
                if existing.snapshot == snapshot:
                    return existing.model_copy(deep=True)
                raise RealCompositionConflict("REAL composition already differs")
            envelope = RealCompositionEnvelope.build(
                snapshot, recorded_at=self._clock.now()
            )
            self._redactor.assert_safe_model(envelope)
            self._items[run_id] = envelope.model_copy(deep=True)
            return envelope.model_copy(deep=True)

    def load(self, run_id: str) -> RealCompositionEnvelope:
        with self._lock:
            try:
                value = self._items[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise RealCompositionNotFound(run_id) from exc
        try:
            self._redactor.assert_safe_model(value)
            return RealCompositionEnvelope.model_validate(
                value.model_dump(mode="python")
            )
        except (ValueError, UnsafePersistenceData) as exc:
            raise RealCompositionCorruption(
                "REAL composition authority is invalid"
            ) from exc

    def _validate_snapshot(self, snapshot: RealCompositionSnapshot) -> None:
        try:
            self._redactor.assert_safe_model(snapshot)
            RealCompositionSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except (ValueError, UnsafePersistenceData) as exc:
            raise RealCompositionCorruption(
                "REAL composition snapshot is invalid"
            ) from exc
