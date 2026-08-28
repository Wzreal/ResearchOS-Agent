from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from test_phase5_evidence_claims import browser, build_memory, context, observation

from researchos.adapters import _atomic_file
from researchos.adapters._snapshot_jsonl import canonical_dict_bytes
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.application.errors import (
    ClaimGraphPersistenceError,
    CorruptEvidenceStore,
    EvidencePersistenceError,
    UnsafePersistenceData,
)
from researchos.application.evidence_extractor import stable_id
from researchos.domain.claims import ClaimGraphSnapshot
from researchos.domain.evidence import (
    EvidenceStoreSnapshot,
    SourceRecord,
    SourceType,
)


def _populated_evidence_snapshot() -> EvidenceStoreSnapshot:
    memory, store, _ = build_memory()
    ctx = context()
    asyncio.run(memory.ingest_observation(ctx, observation(browser("content"))))
    return store.load(ctx.run_id)


@pytest.mark.parametrize(
    ("store_type", "snapshot", "filename"),
    [
        (
            FilesystemEvidenceStore,
            EvidenceStoreSnapshot(run_id="run_a", store_revision=0),
            "evidence.jsonl",
        ),
        (
            FilesystemClaimGraphStore,
            ClaimGraphSnapshot(run_id="run_a", store_revision=0),
            "claims.jsonl",
        ),
    ],
)
def test_phase5_snapshot_bytes_are_deterministic(
    store_type, snapshot, filename, tmp_path
) -> None:
    first = store_type(tmp_path / "first")
    second = store_type(tmp_path / "second")
    first.create(snapshot)
    second.create(snapshot)
    assert (tmp_path / "first" / "run_a" / filename).read_bytes() == (
        tmp_path / "second" / "run_a" / filename
    ).read_bytes()
    data = (tmp_path / "first" / "run_a" / filename).read_bytes()
    header_line, payload = data.split(b"\n", 1)
    header = json.loads(header_line)
    assert header["run_id"] == "run_a"
    assert header["store_revision"] == 0
    assert header["record_count"] == len(payload.splitlines())
    assert header["payload_sha256"] == hashlib.sha256(payload).hexdigest()


def test_evidence_snapshot_record_order_is_canonical_not_caller_order(tmp_path) -> None:
    snapshot = _populated_evidence_snapshot()
    reversed_snapshot = snapshot.model_copy(
        update={
            "sources": tuple(reversed(snapshot.sources)),
            "evidence": tuple(reversed(snapshot.evidence)),
            "revisions": tuple(reversed(snapshot.revisions)),
            "receipts": tuple(reversed(snapshot.receipts)),
        }
    )
    first = FilesystemEvidenceStore(tmp_path / "first")
    second = FilesystemEvidenceStore(tmp_path / "second")
    first.create(snapshot)
    second.create(reversed_snapshot)
    assert (tmp_path / "first" / snapshot.run_id / "evidence.jsonl").read_bytes() == (
        tmp_path / "second" / snapshot.run_id / "evidence.jsonl"
    ).read_bytes()


@pytest.mark.parametrize(
    "fault_point", ["before_temp_write", "after_temp_fsync", "before_replace"]
)
def test_atomic_failure_before_replace_never_publishes_snapshot(
    fault_point, tmp_path
) -> None:
    def fault(point: str) -> None:
        if point == fault_point:
            raise OSError("injected failure")

    store = FilesystemEvidenceStore(tmp_path, fault_injector=fault)
    with pytest.raises(EvidencePersistenceError) as caught:
        store.create(EvidenceStoreSnapshot(run_id="run_a", store_revision=0))
    assert caught.value.store_replaced is False
    assert not (tmp_path / "run_a" / "evidence.jsonl").exists()
    assert not tuple((tmp_path / "run_a").glob("*.tmp"))


def test_os_replace_failure_reports_not_replaced(monkeypatch, tmp_path) -> None:
    def fail_replace(source, target) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(_atomic_file.os, "replace", fail_replace)
    store = FilesystemClaimGraphStore(tmp_path)
    with pytest.raises(ClaimGraphPersistenceError) as caught:
        store.create(ClaimGraphSnapshot(run_id="run_a", store_revision=0))
    assert caught.value.store_replaced is False
    assert not (tmp_path / "run_a" / "claims.jsonl").exists()


def test_failed_save_before_replace_preserves_old_authoritative_snapshot(
    tmp_path,
) -> None:
    class ToggleFault:
        enabled = False

        def __call__(self, point: str) -> None:
            if self.enabled and point == "before_replace":
                raise OSError("before replace")

    fault = ToggleFault()
    store = FilesystemEvidenceStore(tmp_path, fault_injector=fault)
    original = EvidenceStoreSnapshot(run_id="run_a", store_revision=0)
    store.create(original)
    fault.enabled = True
    with pytest.raises(EvidencePersistenceError) as caught:
        store.save(
            original.model_copy(update={"store_revision": 1}), expected_revision=0
        )
    assert caught.value.store_replaced is False
    fault.enabled = False
    assert store.load("run_a") == original


def test_failure_after_parent_fsync_reports_replaced_and_new_snapshot_loads(
    tmp_path,
) -> None:
    class ToggleFault:
        enabled = False

        def __call__(self, point: str) -> None:
            if self.enabled and point == "after_parent_fsync":
                raise OSError("after parent fsync")

    fault = ToggleFault()
    store = FilesystemEvidenceStore(tmp_path, fault_injector=fault)
    original = EvidenceStoreSnapshot(run_id="run_a", store_revision=0)
    store.create(original)
    fault.enabled = True
    updated = original.model_copy(update={"store_revision": 1})
    with pytest.raises(EvidencePersistenceError) as caught:
        store.save(updated, expected_revision=0)
    assert caught.value.store_replaced is True
    fault.enabled = False
    assert store.load("run_a") == updated


def test_windows_parent_directory_fsync_open_failure_is_best_effort(
    monkeypatch, tmp_path
) -> None:
    def fail_open(path, flags):
        del path, flags
        raise OSError("directory handles may not support fsync")

    monkeypatch.setattr(_atomic_file.os, "open", fail_open)
    _atomic_file.fsync_parent(tmp_path)


@pytest.mark.parametrize("corruption", ["middle", "tail", "hash"])
def test_evidence_snapshot_corruption_is_rejected(corruption, tmp_path) -> None:
    snapshot = _populated_evidence_snapshot()
    store = FilesystemEvidenceStore(tmp_path)
    store.create(snapshot)
    path = tmp_path / snapshot.run_id / "evidence.jsonl"
    data = path.read_bytes()
    if corruption == "middle":
        marker = b'"record_type":"evidence"'
        data = data.replace(marker, b'"record_type":"evidencf"', 1)
    elif corruption == "tail":
        data += b'{"incomplete"'
    else:
        header, remainder = data.split(b"\n", 1)
        parsed = json.loads(header)
        parsed["payload_sha256"] = "0" * 64
        data = canonical_dict_bytes(parsed) + b"\n" + remainder
    path.write_bytes(data)
    with pytest.raises(CorruptEvidenceStore):
        store.load(snapshot.run_id)


def test_wrong_run_identity_is_rejected(tmp_path) -> None:
    source = FilesystemEvidenceStore(tmp_path)
    source.create(EvidenceStoreSnapshot(run_id="run_a", store_revision=0))
    wrong_dir = tmp_path / "run_b"
    wrong_dir.mkdir()
    (wrong_dir / "evidence.jsonl").write_bytes(
        (tmp_path / "run_a" / "evidence.jsonl").read_bytes()
    )
    with pytest.raises(CorruptEvidenceStore):
        source.load("run_b")


def test_duplicate_persisted_record_is_rejected_even_with_matching_envelope_hash(
    tmp_path,
) -> None:
    snapshot = _populated_evidence_snapshot()
    store = FilesystemEvidenceStore(tmp_path)
    store.create(snapshot)
    path = tmp_path / snapshot.run_id / "evidence.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    duplicate = next(line for line in lines[1:] if b'"record_type":"evidence"' in line)
    payload = b"".join((*lines[1:], duplicate))
    header["record_count"] += 1
    header["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_bytes(canonical_dict_bytes(header) + b"\n" + payload)
    with pytest.raises(CorruptEvidenceStore):
        store.load(snapshot.run_id)


def test_store_revalidates_current_pointer_and_cross_references(tmp_path) -> None:
    snapshot = _populated_evidence_snapshot()
    record = snapshot.evidence[0].model_copy(update={"current_revision": 99})
    corrupted = snapshot.model_copy(update={"evidence": (record,)})
    with pytest.raises(CorruptEvidenceStore):
        FilesystemEvidenceStore(tmp_path).create(corrupted)


def test_filesystem_store_rejects_model_that_requires_redaction(tmp_path) -> None:
    canonical_locator = "https://example.com/"
    source = SourceRecord(
        run_id="run_a",
        source_id=stable_id("src", ["run_a", SourceType.WEB.value, canonical_locator]),
        source_type=SourceType.WEB,
        canonical_locator=canonical_locator,
        original_locator="Bearer abcdefghijklmnop",
        first_seen_at=datetime.now(UTC),
        first_adapter_id="adapter_a",
    )
    snapshot = EvidenceStoreSnapshot(
        run_id="run_a", store_revision=0, sources=(source,)
    )
    with pytest.raises(UnsafePersistenceData):
        FilesystemEvidenceStore(tmp_path).create(snapshot)
