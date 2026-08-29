from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_phase5_evidence_claims import (
    browser,
    build_memory,
    context,
    graph_services,
    observation,
)

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
from researchos.domain.claims import ClaimEvidenceRelation, ClaimGraphSnapshot
from researchos.domain.evidence import (
    EvidenceRevisionRef,
    EvidenceStoreSnapshot,
    SourceRecord,
    SourceType,
)


def _populated_evidence_snapshot() -> EvidenceStoreSnapshot:
    memory, store, _ = build_memory()
    ctx = context()
    asyncio.run(memory.ingest_observation(ctx, observation(browser("content"))))
    return store.load(ctx.run_id)


def _three_revision_snapshots():
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    first = None
    for number, content in enumerate(("one", "two", "three"), start=1):
        first = asyncio.run(
            memory.ingest_observation(
                ctx.model_copy(
                    update={"attempt_id": f"attempt_{number}", "attempt_number": number}
                ),
                observation(
                    browser(content),
                    call_id=f"call_{number}",
                ),
            )
        )
    assert first is not None
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="one",
    )
    for expected, statement in ((1, "two"), (2, "three")):
        graph.revise_claim(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            statement=statement,
            expected_revision=expected,
        )
    evidence_id = first.items[0].evidence_id
    for relation in (
        ClaimEvidenceRelation.SUPPORTS,
        ClaimEvidenceRelation.CONTRADICTS,
        ClaimEvidenceRelation.CONTEXTUALIZES,
    ):
        graph.relate(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            evidence_id=evidence_id,
            relation=relation,
        )
    return evidence_store.load(ctx.run_id), claim_store.load(ctx.run_id)


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

    monkeypatch.setattr(_atomic_file, "_is_windows", lambda: True)
    monkeypatch.setattr(_atomic_file.os, "open", fail_open)
    _atomic_file.fsync_parent(tmp_path)


def test_non_windows_parent_directory_fsync_open_failure_fails_closed(
    monkeypatch, tmp_path
) -> None:
    def fail_open(path, flags):
        del path, flags
        raise OSError("directory fsync failed")

    monkeypatch.setattr(_atomic_file, "_is_windows", lambda: False)
    monkeypatch.setattr(_atomic_file.os, "open", fail_open)
    with pytest.raises(OSError, match="directory fsync failed"):
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


@pytest.mark.parametrize("missing_revision", [2])
def test_evidence_snapshot_rejects_missing_middle_revision(
    missing_revision,
) -> None:
    evidence, _ = _three_revision_snapshots()
    corrupt = evidence.model_copy(
        update={
            "revisions": tuple(
                item for item in evidence.revisions if item.revision != missing_revision
            )
        }
    )
    with pytest.raises(ValidationError):
        EvidenceStoreSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_evidence_snapshot_rejects_current_pointer_below_maximum() -> None:
    evidence, _ = _three_revision_snapshots()
    record = evidence.evidence[0].model_copy(update={"current_revision": 2})
    corrupt = evidence.model_copy(update={"evidence": (record,)})
    with pytest.raises(ValidationError):
        EvidenceStoreSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_evidence_snapshot_rejects_duplicate_receipt_identity() -> None:
    evidence, _ = _three_revision_snapshots()
    corrupt = evidence.model_copy(
        update={"receipts": (*evidence.receipts, evidence.receipts[0])}
    )
    with pytest.raises(ValidationError):
        EvidenceStoreSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_evidence_snapshot_rejects_receipt_with_missing_revision() -> None:
    evidence, _ = _three_revision_snapshots()
    receipt = evidence.receipts[0].model_copy(
        update={
            "evidence_refs": (
                EvidenceRevisionRef(
                    evidence_id=evidence.evidence[0].evidence_id,
                    revision=99,
                ),
            )
        }
    )
    corrupt = evidence.model_copy(
        update={"receipts": (receipt, *evidence.receipts[1:])}
    )
    with pytest.raises(ValidationError):
        EvidenceStoreSnapshot.model_validate(corrupt.model_dump(mode="python"))


@pytest.mark.parametrize("record_type", ["claim", "edge"])
def test_claim_snapshot_rejects_missing_middle_revision(record_type) -> None:
    _, claims = _three_revision_snapshots()
    field = "claim_revisions" if record_type == "claim" else "edge_revisions"
    records = getattr(claims, field)
    corrupt = claims.model_copy(
        update={field: tuple(item for item in records if item.revision != 2)}
    )
    with pytest.raises(ValidationError):
        ClaimGraphSnapshot.model_validate(corrupt.model_dump(mode="python"))


@pytest.mark.parametrize("record_type", ["claim", "edge"])
def test_claim_snapshot_rejects_current_pointer_below_maximum(record_type) -> None:
    _, claims = _three_revision_snapshots()
    field = "claims" if record_type == "claim" else "edges"
    records = getattr(claims, field)
    changed = records[0].model_copy(update={"current_revision": 2})
    corrupt = claims.model_copy(update={field: (changed,)})
    with pytest.raises(ValidationError):
        ClaimGraphSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_edge_revision_rejects_missing_claim_revision_pin() -> None:
    _, claims = _three_revision_snapshots()
    changed = claims.edge_revisions[-1].model_copy(update={"claim_revision": 99})
    corrupt = claims.model_copy(
        update={
            "edge_revisions": (*claims.edge_revisions[:-1], changed),
        }
    )
    with pytest.raises(ValidationError):
        ClaimGraphSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_claim_snapshot_rejects_duplicate_receipt() -> None:
    _, claims = _three_revision_snapshots()
    corrupt = claims.model_copy(
        update={"receipts": (*claims.receipts, claims.receipts[0])}
    )
    with pytest.raises(ValidationError):
        ClaimGraphSnapshot.model_validate(corrupt.model_dump(mode="python"))


def test_claim_snapshot_rejects_ambiguous_mutation_operation() -> None:
    _, claims = _three_revision_snapshots()
    original = claims.receipts[0]
    request_hash = "f" * 64
    ambiguous = original.model_copy(
        update={
            "request_hash": request_hash,
            "receipt_id": stable_id("cmr", [original.operation_key, request_hash]),
        }
    )
    corrupt = claims.model_copy(update={"receipts": (*claims.receipts, ambiguous)})
    with pytest.raises(ValidationError):
        ClaimGraphSnapshot.model_validate(corrupt.model_dump(mode="python"))
