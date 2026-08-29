"""Single-process claim graph snapshot store with CAS revisions."""

from threading import RLock

from pydantic import ValidationError

from researchos.application.errors import (
    ClaimGraphAlreadyExists,
    ClaimGraphNotFound,
    ClaimGraphRevisionConflict,
    CorruptClaimGraph,
)
from researchos.domain.claims import ClaimGraphSnapshot
from researchos.domain.contracts import model_sha256
from researchos.security.redaction import PersistenceRedactor


class InMemoryClaimGraphStore:
    def __init__(self) -> None:
        self._items: dict[str, ClaimGraphSnapshot] = {}
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: ClaimGraphSnapshot) -> None:
        self._validate(snapshot)
        with self._lock:
            if snapshot.run_id in self._items:
                raise ClaimGraphAlreadyExists(snapshot.run_id)
            self._items[snapshot.run_id] = snapshot.model_copy(deep=True)

    def load(self, run_id: str) -> ClaimGraphSnapshot:
        with self._lock:
            try:
                snapshot = self._items[run_id].model_copy(deep=True)
            except KeyError as exc:
                raise ClaimGraphNotFound(run_id) from exc
        self._validate(snapshot)
        if snapshot.run_id != run_id:
            raise ClaimGraphNotFound(run_id)
        return snapshot

    def snapshot_fingerprint(self, run_id: str) -> tuple[int, str]:
        with self._lock:
            try:
                snapshot = self._items[run_id]
            except KeyError as exc:
                raise ClaimGraphNotFound(run_id) from exc
            return snapshot.store_revision, model_sha256(snapshot)

    def save(self, snapshot: ClaimGraphSnapshot, *, expected_revision: int) -> None:
        self._validate(snapshot)
        with self._lock:
            try:
                current = self._items[snapshot.run_id]
            except KeyError as exc:
                raise ClaimGraphNotFound(snapshot.run_id) from exc
            if current.store_revision != expected_revision:
                raise ClaimGraphRevisionConflict("claim graph revision conflict")
            if snapshot.store_revision != expected_revision + 1:
                raise ClaimGraphRevisionConflict(
                    "claim graph revision must increase by one"
                )
            self._items[snapshot.run_id] = snapshot.model_copy(deep=True)

    def _validate(self, snapshot: ClaimGraphSnapshot) -> None:
        self._redactor.assert_safe_model(snapshot)
        try:
            ClaimGraphSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except ValidationError as exc:
            raise CorruptClaimGraph("claim graph invariants differ") from exc
