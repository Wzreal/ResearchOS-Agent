"""Single-process evidence snapshot store with compare-and-swap revisions."""

from threading import RLock

from pydantic import ValidationError

from researchos.application.errors import (
    CorruptEvidenceStore,
    EvidenceStoreAlreadyExists,
    EvidenceStoreNotFound,
    EvidenceStoreRevisionConflict,
)
from researchos.domain.evidence import EvidenceStoreSnapshot
from researchos.security.redaction import PersistenceRedactor


class InMemoryEvidenceStore:
    def __init__(self) -> None:
        self._items: dict[str, EvidenceStoreSnapshot] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: EvidenceStoreSnapshot) -> None:
        self._validate(snapshot)
        with self._lock:
            if snapshot.run_id in self._items:
                raise EvidenceStoreAlreadyExists(snapshot.run_id)
            self._items[snapshot.run_id] = snapshot.model_copy(deep=True)

    def load(self, run_id: str) -> EvidenceStoreSnapshot:
        with self._lock:
            try:
                snapshot = self._items[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise EvidenceStoreNotFound(run_id) from exc
        self._validate(snapshot)
        if snapshot.run_id != run_id:
            raise EvidenceStoreNotFound(run_id)
        return snapshot

    def save(self, snapshot: EvidenceStoreSnapshot, *, expected_revision: int) -> None:
        self._validate(snapshot)
        with self._lock:
            try:
                current = self._items[snapshot.run_id]
            except KeyError as exc:
                raise EvidenceStoreNotFound(snapshot.run_id) from exc
            if current.store_revision != expected_revision:
                raise EvidenceStoreRevisionConflict(
                    "evidence snapshot revision conflict"
                )
            if snapshot.store_revision != expected_revision + 1:
                raise EvidenceStoreRevisionConflict(
                    "evidence revision must increase by one"
                )
            self._items[snapshot.run_id] = snapshot.model_copy(deep=True)

    def _validate(self, snapshot: EvidenceStoreSnapshot) -> None:
        self._redactor.assert_safe_model(snapshot)
        try:
            EvidenceStoreSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except ValidationError as exc:
            raise CorruptEvidenceStore("evidence snapshot invariants differ") from exc
