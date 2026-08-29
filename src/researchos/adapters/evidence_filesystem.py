"""Atomic, deterministic filesystem evidence snapshots."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from researchos.adapters._atomic_file import AtomicWriteFailure, atomic_replace_bytes
from researchos.adapters._snapshot_jsonl import decode_snapshot, encode_snapshot
from researchos.application.errors import (
    CorruptEvidenceStore,
    EvidencePersistenceError,
    EvidenceStoreAlreadyExists,
    EvidenceStoreNotFound,
    EvidenceStoreRevisionConflict,
    UnsafePersistenceData,
)
from researchos.domain.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceRecord,
    EvidenceRevision,
    EvidenceStoreSnapshot,
    IngestionReceipt,
    SourceRecord,
)
from researchos.security.redaction import PersistenceRedactor

FaultInjector = Callable[[str], None]


def _no_fault(_: str) -> None:
    pass


class FilesystemEvidenceStore:
    filename = "evidence.jsonl"

    def __init__(
        self, root: str | Path, *, fault_injector: FaultInjector | None = None
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault = fault_injector or _no_fault
        self._lock = RLock()
        self._redactor = PersistenceRedactor()

    def create(self, snapshot: EvidenceStoreSnapshot) -> None:
        self._validate(snapshot)
        with self._lock:
            path = self._path(snapshot.run_id)
            if path.exists():
                raise EvidenceStoreAlreadyExists(snapshot.run_id)
            self._write(path, snapshot)

    def load(self, run_id: str) -> EvidenceStoreSnapshot:
        with self._lock:
            path = self._path(run_id)
            if not path.exists():
                raise EvidenceStoreNotFound(run_id)
            try:
                persisted = path.read_bytes()
                header, records = decode_snapshot(persisted)
                if (
                    header.get("run_id") != run_id
                    or header.get("schema_version") != EVIDENCE_SCHEMA_VERSION
                ):
                    raise ValueError("snapshot identity/schema differs")
                groups: dict[str, list[object]] = {
                    "source": [],
                    "evidence": [],
                    "revision": [],
                    "receipt": [],
                }
                models = {
                    "source": SourceRecord,
                    "evidence": EvidenceRecord,
                    "revision": EvidenceRevision,
                    "receipt": IngestionReceipt,
                }
                for kind, payload in records:
                    if kind not in models:
                        raise ValueError("unknown evidence record type")
                    groups[kind].append(models[kind].model_validate(payload))
                snapshot = EvidenceStoreSnapshot(
                    run_id=run_id,
                    store_revision=header["store_revision"],
                    sources=tuple(groups["source"]),
                    evidence=tuple(groups["evidence"]),
                    revisions=tuple(groups["revision"]),
                    receipts=tuple(groups["receipt"]),
                )
                self._validate(snapshot)
                if self._encode(snapshot) != persisted:
                    raise ValueError("evidence snapshot is not canonically ordered")
                return snapshot
            except (
                OSError,
                UnicodeError,
                ValueError,
                KeyError,
                ValidationError,
                UnsafePersistenceData,
            ) as exc:
                raise CorruptEvidenceStore("evidence snapshot is invalid") from exc

    def save(self, snapshot: EvidenceStoreSnapshot, *, expected_revision: int) -> None:
        self._validate(snapshot)
        with self._lock:
            current = self.load(snapshot.run_id)
            if (
                current.store_revision != expected_revision
                or snapshot.store_revision != expected_revision + 1
            ):
                raise EvidenceStoreRevisionConflict(
                    "evidence snapshot revision conflict"
                )
            self._write(self._path(snapshot.run_id), snapshot)

    def _write(self, path: Path, snapshot: EvidenceStoreSnapshot) -> None:
        data = self._encode(snapshot)
        try:
            atomic_replace_bytes(path, data, fault=self._fault)
        except AtomicWriteFailure as exc:
            raise EvidencePersistenceError(
                "atomic evidence write failed",
                run_id=snapshot.run_id,
                store_replaced=exc.replaced,
            ) from exc

    @staticmethod
    def _encode(snapshot: EvidenceStoreSnapshot) -> bytes:
        records = [
            *(
                ("source", item)
                for item in sorted(snapshot.sources, key=lambda x: x.source_id)
            ),
            *(
                ("evidence", item)
                for item in sorted(snapshot.evidence, key=lambda x: x.evidence_id)
            ),
            *(
                ("revision", item)
                for item in sorted(
                    snapshot.revisions, key=lambda x: (x.evidence_id, x.revision)
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
            schema_version=EVIDENCE_SCHEMA_VERSION,
            records=records,
        )

    def _validate(self, snapshot: EvidenceStoreSnapshot) -> None:
        self._redactor.assert_safe_model(snapshot)
        try:
            EvidenceStoreSnapshot.model_validate(snapshot.model_dump(mode="python"))
        except ValidationError as exc:
            raise CorruptEvidenceStore("evidence snapshot invariants differ") from exc

    def _path(self, run_id: str) -> Path:
        if not run_id or any(
            ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in run_id
        ):
            raise ValueError("unsafe run ID")
        run_dir = (self.root / run_id).resolve()
        if run_dir.parent != self.root:
            raise ValueError("evidence path escapes output root")
        return run_dir / self.filename
