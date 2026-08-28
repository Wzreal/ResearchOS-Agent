"""Atomic, deterministic filesystem claim graph snapshots."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.adapters._snapshot_jsonl import decode_snapshot, encode_snapshot
from researchos.application.errors import (
    ClaimGraphAlreadyExists,
    ClaimGraphNotFound,
    ClaimGraphPersistenceError,
    ClaimGraphRevisionConflict,
    CorruptClaimGraph,
    UnsafePersistenceData,
)
from researchos.domain.claims import (
    CLAIM_SCHEMA_VERSION,
    ClaimEvidenceEdge,
    ClaimEvidenceEdgeRevision,
    ClaimGraphSnapshot,
    ClaimMutationReceipt,
    ClaimRecord,
    ClaimRevision,
)
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]


def _no_fault(_: str) -> None:
    pass


class FilesystemClaimGraphStore:
    filename = "claims.jsonl"

    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: ClaimGraphSnapshot) -> None:
        self._validate(snapshot)
        with self._lock:
            path = self._path(snapshot.run_id)
            if path.exists():
                raise ClaimGraphAlreadyExists(snapshot.run_id)
            self._write(path, snapshot)

    def load(self, run_id: str) -> ClaimGraphSnapshot:
        with self._lock:
            path = self._path(run_id)
            if not path.exists():
                raise ClaimGraphNotFound(run_id)
            try:
                persisted = path.read_bytes()
                header, records = decode_snapshot(persisted)
                if (
                    header.get("run_id") != run_id
                    or header.get("schema_version") != CLAIM_SCHEMA_VERSION
                ):
                    raise ValueError("snapshot identity/schema differs")
                groups: dict[str, list[object]] = {
                    "claim": [],
                    "claim_revision": [],
                    "edge": [],
                    "edge_revision": [],
                    "receipt": [],
                }
                models = {
                    "claim": ClaimRecord,
                    "claim_revision": ClaimRevision,
                    "edge": ClaimEvidenceEdge,
                    "edge_revision": ClaimEvidenceEdgeRevision,
                    "receipt": ClaimMutationReceipt,
                }
                for kind, payload in records:
                    if kind not in models:
                        raise ValueError("unknown claim record type")
                    groups[kind].append(models[kind].model_validate(payload))
                snapshot = ClaimGraphSnapshot(
                    run_id=run_id,
                    store_revision=header["store_revision"],
                    claims=tuple(groups["claim"]),
                    claim_revisions=tuple(groups["claim_revision"]),
                    edges=tuple(groups["edge"]),
                    edge_revisions=tuple(groups["edge_revision"]),
                    receipts=tuple(groups["receipt"]),
                )
                self._validate(snapshot)
                if self._encode(snapshot) != persisted:
                    raise ValueError("claim graph snapshot is not canonically ordered")
                return snapshot
            except (
                OSError,
                UnicodeError,
                ValueError,
                KeyError,
                ValidationError,
                UnsafePersistenceData,
            ) as exc:
                raise CorruptClaimGraph("claim graph snapshot is invalid") from exc

    def save(self, snapshot: ClaimGraphSnapshot, *, expected_revision: int) -> None:
        self._validate(snapshot)
        with self._lock:
            current = self.load(snapshot.run_id)
            if (
                current.store_revision != expected_revision
                or snapshot.store_revision != expected_revision + 1
            ):
                raise ClaimGraphRevisionConflict("claim graph revision conflict")
            self._write(self._path(snapshot.run_id), snapshot)

    def _write(self, path: Path, snapshot: ClaimGraphSnapshot) -> None:
        data = self._encode(snapshot)
        try:
            atomic_replace_bytes(path, data, fault=self._fault)
        except AtomicWriteFailure as exc:
            raise ClaimGraphPersistenceError(
                "atomic claim graph write failed",
                run_id=snapshot.run_id,
                store_replaced=exc.replaced,
            ) from exc

    @staticmethod
    def _encode(snapshot: ClaimGraphSnapshot) -> bytes:
        records = [
            *(
                ("claim", item)
                for item in sorted(snapshot.claims, key=lambda x: x.claim_id)
            ),
            *(
                ("claim_revision", item)
                for item in sorted(
                    snapshot.claim_revisions, key=lambda x: (x.claim_id, x.revision)
                )
            ),
            *(
                ("edge", item)
                for item in sorted(snapshot.edges, key=lambda x: x.edge_id)
            ),
            *(
                ("edge_revision", item)
                for item in sorted(
                    snapshot.edge_revisions, key=lambda x: (x.edge_id, x.revision)
                )
            ),
            *(
                ("receipt", item)
                for item in sorted(snapshot.receipts, key=lambda x: x.receipt_id)
            ),
        ]
        return encode_snapshot(
            run_id=snapshot.run_id,
            store_revision=snapshot.store_revision,
            schema_version=CLAIM_SCHEMA_VERSION,
            records=records,
        )

    def _validate(self, snapshot: ClaimGraphSnapshot) -> None:
        self._redactor.assert_safe_model(snapshot)
        try:
            ClaimGraphSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except ValidationError as exc:
            raise CorruptClaimGraph("claim graph invariants differ") from exc

    def _path(self, run_id: str) -> Path:
        if not run_id or any(
            ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in run_id
        ):
            raise ValueError("unsafe run ID")
        run_dir = (self.root / run_id).resolve()
        if run_dir.parent != self.root:
            raise ValueError("claim graph path escapes output root")
        return run_dir / self.filename
