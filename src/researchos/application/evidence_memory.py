"""Phase 5 application-owned evidence ingestion and idempotency semantics."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import suppress

from researchos.application.errors import (
    EvidenceIdempotencyConflict,
    EvidenceStoreAlreadyExists,
    EvidenceStoreNotFound,
    EvidenceTraceCommitError,
)
from researchos.application.evidence_extractor import EvidenceExtractor
from researchos.domain.agent import AgentContext, AgentObservation
from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.evidence import (
    EvidenceCandidate,
    EvidenceIngestionDisposition,
    EvidenceIngestionItemResult,
    EvidenceIngestionResult,
    EvidenceRecord,
    EvidenceRevision,
    EvidenceRevisionRef,
    EvidenceStoreSnapshot,
    EvidenceView,
    IngestionReceipt,
    RecordLifecycle,
    SourceRecord,
)
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)
from researchos.interfaces.evidence import EvidenceStore
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.security.redaction import PersistenceRedactor


class EvidenceMemory:
    def __init__(
        self,
        *,
        store: EvidenceStore,
        clock: Clock,
        trace_sink: TraceSink,
        extractor: EvidenceExtractor | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._trace = trace_sink
        self._extractor = extractor or EvidenceExtractor()
        self._redactor = redactor or PersistenceRedactor()
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def ingest_observation(
        self, context: AgentContext, observation: AgentObservation
    ) -> EvidenceIngestionResult:
        candidates = self._extractor.extract(context, observation)
        if not candidates:
            return EvidenceIngestionResult(
                run_id=context.run_id,
                ingestion_operation_key=stable_hash(
                    [observation.tool_operation_key, "no-eligible-evidence"]
                ),
                receipt_id=stable_id(
                    "receipt", [context.attempt_id, observation.tool_call_id, "empty"]
                ),
                store_revision=self._current_revision(context.run_id),
                items=(),
            )
        async with self._locks[context.run_id]:
            return self.ingest(candidates)

    def ingest(
        self, candidates: tuple[EvidenceCandidate, ...]
    ) -> EvidenceIngestionResult:
        if not candidates:
            raise ValueError("evidence ingestion requires at least one candidate")
        first = candidates[0]
        if any(
            (item.run_id, item.task_operation_key, item.tool_operation_key)
            != (first.run_id, first.task_operation_key, first.tool_operation_key)
            for item in candidates
        ):
            raise ValueError("candidate batch must describe one logical Tool result")
        for item in candidates:
            self._redactor.assert_safe_model(item)
        stable_payload = tuple(
            sorted(
                (
                    item.source_type.value,
                    item.canonical_locator,
                    item.evidence_scope_key,
                    item.media_type,
                    sha256_text(item.content),
                    stable_hash(item.extraction_context),
                    item.extractor_id,
                    item.extractor_version,
                )
                for item in candidates
            )
        )
        request_hash = stable_hash(stable_payload)
        operation_key = stable_hash(
            [first.task_operation_key, first.tool_operation_key, request_hash]
        )
        receipt_id = stable_id(
            "receipt",
            [
                operation_key,
                first.attempt_id,
                first.attempt_number,
                first.agent_step,
                first.tool_call_id,
            ],
        )
        snapshot = self._load_or_create(first.run_id)
        same_operation = [
            item
            for item in snapshot.receipts
            if item.ingestion_operation_key == operation_key
        ]
        if any(item.canonical_request_hash != request_hash for item in same_operation):
            raise EvidenceIdempotencyConflict(
                "ingestion operation was reused with another stable payload"
            )
        existing_receipt = next(
            (item for item in snapshot.receipts if item.receipt_id == receipt_id), None
        )
        if existing_receipt is not None:
            result = self._result_from_receipt(
                snapshot, existing_receipt, operation_key, replayed=True
            )
            try:
                self._emit(first.run_id, first.run_revision, result)
            except Exception as exc:
                raise EvidenceTraceCommitError(
                    "evidence replay trace append failed",
                    run_id=first.run_id,
                    store_revision=snapshot.store_revision,
                ) from exc
            return result

        now = self._clock.now()
        sources = {item.source_id: item for item in snapshot.sources}
        records = {item.evidence_id: item for item in snapshot.evidence}
        revisions = list(snapshot.revisions)
        item_results: list[EvidenceIngestionItemResult] = []
        refs: list[EvidenceRevisionRef] = []
        for candidate in candidates:
            source_id = stable_id(
                "src",
                [
                    candidate.run_id,
                    candidate.source_type.value,
                    candidate.canonical_locator,
                ],
            )
            if source_id not in sources:
                sources[source_id] = SourceRecord(
                    run_id=candidate.run_id,
                    source_id=source_id,
                    source_type=candidate.source_type,
                    canonical_locator=candidate.canonical_locator,
                    original_locator=candidate.original_locator,
                    first_seen_at=candidate.observed_at,
                    first_adapter_id=candidate.adapter_id,
                )
            evidence_id = stable_id(
                "ev",
                [
                    candidate.run_id,
                    source_id,
                    candidate.evidence_scope_key,
                    candidate.media_type,
                ],
            )
            content_hash = sha256_text(candidate.content)
            normalized_hash = sha256_text(normalize_content(candidate.content))
            context_hash = stable_hash(candidate.extraction_context)
            current = records.get(evidence_id)
            current_revision = next(
                (
                    item
                    for item in revisions
                    if current
                    and item.evidence_id == evidence_id
                    and item.revision == current.current_revision
                ),
                None,
            )
            if current is None:
                revision_number = 1
                disposition = EvidenceIngestionDisposition.CREATED
                revisions.append(
                    EvidenceRevision(
                        run_id=candidate.run_id,
                        evidence_id=evidence_id,
                        revision=1,
                        content=candidate.content,
                        content_hash=content_hash,
                        normalized_content_hash=normalized_hash,
                        extraction_context=candidate.extraction_context,
                        extraction_context_hash=context_hash,
                        extractor_id=candidate.extractor_id,
                        extractor_version=candidate.extractor_version,
                        created_at=now,
                    )
                )
                records[evidence_id] = EvidenceRecord(
                    run_id=candidate.run_id,
                    evidence_id=evidence_id,
                    source_id=source_id,
                    evidence_scope_key=candidate.evidence_scope_key,
                    media_type=candidate.media_type,
                    current_revision=1,
                    created_at=now,
                    updated_at=now,
                )
            elif (
                current_revision is not None
                and current_revision.content_hash == content_hash
                and current_revision.extraction_context_hash == context_hash
            ):
                revision_number = current.current_revision
                disposition = EvidenceIngestionDisposition.DEDUPLICATED
            else:
                revision_number = current.current_revision + 1
                disposition = EvidenceIngestionDisposition.REVISED
                revisions.append(
                    EvidenceRevision(
                        run_id=candidate.run_id,
                        evidence_id=evidence_id,
                        revision=revision_number,
                        content=candidate.content,
                        content_hash=content_hash,
                        normalized_content_hash=normalized_hash,
                        extraction_context=candidate.extraction_context,
                        extraction_context_hash=context_hash,
                        extractor_id=candidate.extractor_id,
                        extractor_version=candidate.extractor_version,
                        created_at=now,
                        supersedes_revision=current.current_revision,
                    )
                )
                records[evidence_id] = current.model_copy(
                    update={
                        "current_revision": revision_number,
                        "lifecycle": RecordLifecycle.ACTIVE,
                        "updated_at": now,
                    }
                )
            refs.append(
                EvidenceRevisionRef(evidence_id=evidence_id, revision=revision_number)
            )
            item_results.append(
                EvidenceIngestionItemResult(
                    evidence_id=evidence_id,
                    revision=revision_number,
                    disposition=disposition,
                )
            )
        receipt = IngestionReceipt(
            receipt_id=receipt_id,
            run_id=first.run_id,
            run_revision=first.run_revision,
            ingestion_operation_key=operation_key,
            canonical_request_hash=request_hash,
            task_id=first.task_id,
            task_operation_key=first.task_operation_key,
            attempt_id=first.attempt_id,
            attempt_number=first.attempt_number,
            agent_step=first.agent_step,
            tool_call_id=first.tool_call_id,
            tool_operation_key=first.tool_operation_key,
            adapter_id=first.adapter_id,
            observed_at=min(item.observed_at for item in candidates),
            recorded_at=now,
            evidence_refs=tuple(refs),
        )
        updated = EvidenceStoreSnapshot(
            run_id=first.run_id,
            store_revision=snapshot.store_revision + 1,
            sources=tuple(sorted(sources.values(), key=lambda x: x.source_id)),
            evidence=tuple(sorted(records.values(), key=lambda x: x.evidence_id)),
            revisions=tuple(
                sorted(revisions, key=lambda x: (x.evidence_id, x.revision))
            ),
            receipts=tuple(
                sorted((*snapshot.receipts, receipt), key=lambda x: x.receipt_id)
            ),
        )
        self._store.save(updated, expected_revision=snapshot.store_revision)
        result = EvidenceIngestionResult(
            run_id=first.run_id,
            ingestion_operation_key=operation_key,
            receipt_id=receipt_id,
            store_revision=updated.store_revision,
            items=tuple(item_results),
        )
        try:
            self._emit(first.run_id, first.run_revision, result)
        except Exception as exc:
            raise EvidenceTraceCommitError(
                "evidence committed but trace append failed",
                run_id=first.run_id,
                store_revision=updated.store_revision,
            ) from exc
        return result

    def tombstone(
        self, run_id: str, evidence_id: str, *, run_revision: int
    ) -> EvidenceRecord:
        snapshot = self._store.load(run_id)
        record = next(
            (item for item in snapshot.evidence if item.evidence_id == evidence_id),
            None,
        )
        if record is None:
            raise EvidenceStoreNotFound(evidence_id)
        updated_record = record.model_copy(
            update={
                "lifecycle": RecordLifecycle.TOMBSTONED,
                "updated_at": self._clock.now(),
            }
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "evidence": tuple(
                    updated_record if item.evidence_id == evidence_id else item
                    for item in snapshot.evidence
                ),
            }
        )
        self._store.save(updated, expected_revision=snapshot.store_revision)
        try:
            self._trace.append(
                TraceEvent(
                    event_id=stable_id(
                        "evt",
                        ["evidence-tombstone", evidence_id, updated.store_revision],
                    ),
                    event_type=TraceEventType.EVIDENCE_TOMBSTONED,
                    timestamp=self._clock.now(),
                    run_id=run_id,
                    revision=run_revision,
                    correlation_id=evidence_id,
                    attributes={
                        "evidence_id": evidence_id,
                        "evidence_revision": updated_record.current_revision,
                        "store_revision": updated.store_revision,
                    },
                )
            )
        except Exception as exc:
            raise EvidenceTraceCommitError(
                "evidence tombstone committed but trace append failed",
                run_id=run_id,
                store_revision=updated.store_revision,
            ) from exc
        return updated_record

    def duplicate_evidence(self, run_id: str, evidence_id: str) -> tuple[str, ...]:
        snapshot = self._store.load(run_id)
        record = next(
            item for item in snapshot.evidence if item.evidence_id == evidence_id
        )
        target = next(
            item
            for item in snapshot.revisions
            if item.evidence_id == evidence_id
            and item.revision == record.current_revision
        )
        current_hashes = {
            item.evidence_id: next(
                revision.normalized_content_hash
                for revision in snapshot.revisions
                if revision.evidence_id == item.evidence_id
                and revision.revision == item.current_revision
            )
            for item in snapshot.evidence
        }
        return tuple(
            sorted(
                item_id
                for item_id, normalized_hash in current_hashes.items()
                if item_id != evidence_id
                and normalized_hash == target.normalized_content_hash
            )
        )

    def get_evidence(
        self,
        run_id: str,
        evidence_id: str,
        *,
        revision: int | None = None,
        include_tombstoned: bool = False,
    ) -> EvidenceView:
        snapshot = self._store.load(run_id)
        record = next(
            (item for item in snapshot.evidence if item.evidence_id == evidence_id),
            None,
        )
        if record is None or (
            record.lifecycle is RecordLifecycle.TOMBSTONED and not include_tombstoned
        ):
            raise EvidenceStoreNotFound(evidence_id)
        selected_revision = revision or record.current_revision
        item = next(
            (
                candidate
                for candidate in snapshot.revisions
                if candidate.evidence_id == evidence_id
                and candidate.revision == selected_revision
            ),
            None,
        )
        if item is None:
            raise EvidenceStoreNotFound(f"{evidence_id} revision {selected_revision}")
        return EvidenceView(evidence=record, revision=item)

    def list_evidence(
        self,
        run_id: str,
        *,
        after_id: str | None = None,
        limit: int = 50,
        include_tombstoned: bool = False,
    ) -> tuple[EvidenceView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        snapshot = self._store.load(run_id)
        records = sorted(
            (
                item
                for item in snapshot.evidence
                if after_id is None or item.evidence_id > after_id
                if include_tombstoned or item.lifecycle is RecordLifecycle.ACTIVE
            ),
            key=lambda item: item.evidence_id,
        )[:limit]
        return tuple(
            EvidenceView(
                evidence=record,
                revision=next(
                    revision
                    for revision in snapshot.revisions
                    if revision.evidence_id == record.evidence_id
                    and revision.revision == record.current_revision
                ),
            )
            for record in records
        )

    def _load_or_create(self, run_id: str) -> EvidenceStoreSnapshot:
        try:
            return self._store.load(run_id)
        except EvidenceStoreNotFound:
            initial = EvidenceStoreSnapshot(run_id=run_id, store_revision=0)
            with suppress(EvidenceStoreAlreadyExists):
                self._store.create(initial)
            return self._store.load(run_id)

    def _current_revision(self, run_id: str) -> int:
        try:
            return self._store.load(run_id).store_revision
        except EvidenceStoreNotFound:
            return 0

    @staticmethod
    def _result_from_receipt(
        snapshot: EvidenceStoreSnapshot,
        receipt: IngestionReceipt,
        operation_key: str,
        *,
        replayed: bool,
    ) -> EvidenceIngestionResult:
        return EvidenceIngestionResult(
            run_id=snapshot.run_id,
            ingestion_operation_key=operation_key,
            receipt_id=receipt.receipt_id,
            store_revision=snapshot.store_revision,
            replayed=replayed,
            items=tuple(
                EvidenceIngestionItemResult(
                    evidence_id=ref.evidence_id,
                    revision=ref.revision,
                    disposition=EvidenceIngestionDisposition.DEDUPLICATED,
                )
                for ref in receipt.evidence_refs
            ),
        )

    def _emit(
        self, run_id: str, run_revision: int, result: EvidenceIngestionResult
    ) -> None:
        dispositions = [item.disposition for item in result.items]
        event_type = TraceEventType.EVIDENCE_REPLAYED
        if not result.replayed:
            event_type = (
                TraceEventType.EVIDENCE_REVISED
                if EvidenceIngestionDisposition.REVISED in dispositions
                else TraceEventType.EVIDENCE_INGESTED
            )
        self._trace.append(
            TraceEvent(
                event_id=stable_id(
                    "evt", [result.ingestion_operation_key, result.store_revision]
                ),
                event_type=event_type,
                timestamp=self._clock.now(),
                run_id=run_id,
                revision=run_revision,
                correlation_id=result.receipt_id,
                attributes={
                    "ingestion_operation_key": result.ingestion_operation_key,
                    "receipt_id": result.receipt_id,
                    "store_revision": result.store_revision,
                    "extractor_id": self._extractor.extractor_id,
                    "extractor_version": self._extractor.extractor_version,
                    "evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "revision": item.revision,
                            "disposition": item.disposition.value,
                        }
                        for item in result.items
                    ],
                },
            )
        )
