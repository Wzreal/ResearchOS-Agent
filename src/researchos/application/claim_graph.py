"""Application service for versioned claims and claim/evidence relations."""

from __future__ import annotations

from contextlib import suppress

from researchos.application.errors import (
    ClaimGraphAlreadyExists,
    ClaimGraphNotFound,
    ClaimGraphPreconditionError,
    ClaimGraphTraceCommitError,
    EvidenceStoreNotFound,
)
from researchos.domain.claims import (
    ClaimEvidenceEdge,
    ClaimEvidenceEdgeRevision,
    ClaimEvidenceRelation,
    ClaimGenerationContext,
    ClaimGraphSnapshot,
    ClaimMutationReceipt,
    ClaimRecord,
    ClaimRevision,
    ClaimView,
    ConflictCandidate,
    EdgeView,
)
from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)
from researchos.interfaces.evidence import ClaimGraphStore, EvidenceStore
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.security.redaction import PersistenceRedactor


class ClaimGraphService:
    def __init__(
        self,
        *,
        store: ClaimGraphStore,
        evidence_store: EvidenceStore,
        clock: Clock,
        trace_sink: TraceSink,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._store = store
        self._evidence_store = evidence_store
        self._clock = clock
        self._trace = trace_sink
        self._redactor = redactor or PersistenceRedactor()

    def create_claim(
        self,
        *,
        run_id: str,
        run_revision: int,
        claim_scope_key: str,
        statement: str,
        generation_context: ClaimGenerationContext | None = None,
        operation_key: str | None = None,
    ) -> ClaimView:
        snapshot = self._load_or_create(run_id)
        claim_id = stable_id("claim", [run_id, claim_scope_key])
        existing = next(
            (item for item in snapshot.claims if item.claim_id == claim_id), None
        )
        normalized_hash = sha256_text(normalize_content(statement))
        context_payload = (
            generation_context.model_dump(mode="json")
            if generation_context is not None
            else None
        )
        request_hash = stable_hash(
            [run_id, claim_scope_key, normalized_hash, context_payload]
        )
        op = operation_key or stable_hash(["create-claim-v1", request_hash])
        receipt = next(
            (item for item in snapshot.receipts if item.operation_key == op), None
        )
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise ClaimGraphPreconditionError(
                    "claim operation key payload conflict"
                )
            result = self._claim_view(
                snapshot, receipt.claim_id, receipt.claim_revision
            )
            self._emit_replay(snapshot, run_revision, receipt)
            return result
        if existing is not None:
            revision = self._claim_view(
                snapshot, claim_id, existing.current_revision
            ).revision
            if revision.normalized_statement_hash != normalized_hash:
                raise ClaimGraphPreconditionError(
                    "claim scope already exists with different content"
                )
            return ClaimView(claim=existing, revision=revision)
        now = self._clock.now()
        record = ClaimRecord(
            run_id=run_id,
            claim_id=claim_id,
            claim_scope_key=claim_scope_key,
            current_revision=1,
            created_at=now,
            updated_at=now,
        )
        revision = ClaimRevision(
            run_id=run_id,
            claim_id=claim_id,
            revision=1,
            statement=statement,
            normalized_statement_hash=normalized_hash,
            generation_context=generation_context,
            created_at=now,
        )
        mutation = ClaimMutationReceipt(
            receipt_id=stable_id("cmr", [op, request_hash]),
            run_id=run_id,
            operation_key=op,
            request_hash=request_hash,
            claim_id=claim_id,
            claim_revision=1,
            recorded_at=now,
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "claims": tuple(
                    sorted((*snapshot.claims, record), key=lambda x: x.claim_id)
                ),
                "claim_revisions": tuple(
                    sorted(
                        (*snapshot.claim_revisions, revision),
                        key=lambda x: (x.claim_id, x.revision),
                    )
                ),
                "receipts": tuple(
                    sorted((*snapshot.receipts, mutation), key=lambda x: x.receipt_id)
                ),
            }
        )
        self._commit(
            updated,
            snapshot.store_revision,
            run_revision,
            TraceEventType.CLAIM_CREATED,
            claim_id,
            {"claim_id": claim_id, "claim_revision": 1, "operation_key": op},
        )
        return ClaimView(claim=record, revision=revision)

    def revise_claim(
        self,
        *,
        run_id: str,
        run_revision: int,
        claim_id: str,
        statement: str,
        expected_revision: int,
        generation_context: ClaimGenerationContext | None = None,
        operation_key: str | None = None,
    ) -> ClaimView:
        snapshot = self._store.load(run_id)
        normalized_hash = sha256_text(normalize_content(statement))
        context_payload = (
            generation_context.model_dump(mode="json")
            if generation_context is not None
            else None
        )
        request_hash = stable_hash(
            [run_id, claim_id, expected_revision, normalized_hash, context_payload]
        )
        op = operation_key or stable_hash(["revise-claim-v1", request_hash])
        receipt = next(
            (item for item in snapshot.receipts if item.operation_key == op), None
        )
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise ClaimGraphPreconditionError(
                    "claim operation key payload conflict"
                )
            result = self._claim_view(
                snapshot, receipt.claim_id, receipt.claim_revision
            )
            self._emit_replay(snapshot, run_revision, receipt)
            return result
        record = next(
            (item for item in snapshot.claims if item.claim_id == claim_id), None
        )
        if record is None or record.current_revision != expected_revision:
            raise ClaimGraphPreconditionError("claim is missing or revision differs")
        current = self._claim_view(snapshot, claim_id, expected_revision).revision
        if (
            current.normalized_statement_hash == normalized_hash
            and current.generation_context == generation_context
        ):
            return ClaimView(claim=record, revision=current)
        now = self._clock.now()
        revision = ClaimRevision(
            run_id=run_id,
            claim_id=claim_id,
            revision=expected_revision + 1,
            statement=statement,
            normalized_statement_hash=normalized_hash,
            generation_context=generation_context,
            created_at=now,
            supersedes_revision=expected_revision,
        )
        changed = record.model_copy(
            update={
                "current_revision": expected_revision + 1,
                "lifecycle": RecordLifecycle.ACTIVE,
                "updated_at": now,
            }
        )
        mutation = ClaimMutationReceipt(
            receipt_id=stable_id("cmr", [op, request_hash]),
            run_id=run_id,
            operation_key=op,
            request_hash=request_hash,
            claim_id=claim_id,
            claim_revision=revision.revision,
            recorded_at=now,
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "claims": tuple(
                    changed if item.claim_id == claim_id else item
                    for item in snapshot.claims
                ),
                "claim_revisions": tuple(
                    sorted(
                        (*snapshot.claim_revisions, revision),
                        key=lambda x: (x.claim_id, x.revision),
                    )
                ),
                "receipts": tuple(
                    sorted((*snapshot.receipts, mutation), key=lambda x: x.receipt_id)
                ),
            }
        )
        self._commit(
            updated,
            snapshot.store_revision,
            run_revision,
            TraceEventType.CLAIM_REVISED,
            claim_id,
            {
                "claim_id": claim_id,
                "claim_revision": revision.revision,
                "operation_key": op,
            },
        )
        return ClaimView(claim=changed, revision=revision)

    def relate(
        self,
        *,
        run_id: str,
        run_revision: int,
        claim_id: str,
        evidence_id: str,
        relation: ClaimEvidenceRelation,
        claim_revision: int | None = None,
        evidence_revision: int | None = None,
        operation_key: str | None = None,
    ) -> EdgeView:
        snapshot = self._load_or_create(run_id)
        claim = next(
            (item for item in snapshot.claims if item.claim_id == claim_id), None
        )
        if claim is None:
            raise ClaimGraphPreconditionError("claim does not exist")
        try:
            evidence_snapshot = self._evidence_store.load(run_id)
        except EvidenceStoreNotFound as exc:
            raise ClaimGraphPreconditionError("evidence store does not exist") from exc
        evidence = next(
            (
                item
                for item in evidence_snapshot.evidence
                if item.evidence_id == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise ClaimGraphPreconditionError("evidence does not exist")
        claim_rev = claim_revision or claim.current_revision
        evidence_rev = evidence_revision or evidence.current_revision
        if not any(
            item.claim_id == claim_id and item.revision == claim_rev
            for item in snapshot.claim_revisions
        ):
            raise ClaimGraphPreconditionError("claim revision does not exist")
        if not any(
            item.evidence_id == evidence_id and item.revision == evidence_rev
            for item in evidence_snapshot.revisions
        ):
            raise ClaimGraphPreconditionError("evidence revision does not exist")
        edge_id = stable_id("edge", [run_id, claim_id, evidence_id])
        request_hash = stable_hash(
            [
                run_id,
                edge_id,
                relation.value,
                claim_rev,
                evidence_rev,
            ]
        )
        op = operation_key or stable_hash(["relate-v1", request_hash])
        receipt = next(
            (item for item in snapshot.receipts if item.operation_key == op), None
        )
        if receipt is not None:
            if receipt.request_hash != request_hash or receipt.edge_id is None:
                raise ClaimGraphPreconditionError(
                    "relation operation key payload conflict"
                )
            result = EdgeView(
                edge=next(
                    item for item in snapshot.edges if item.edge_id == receipt.edge_id
                ),
                revision=next(
                    item
                    for item in snapshot.edge_revisions
                    if item.edge_id == receipt.edge_id
                    and item.revision == receipt.edge_revision
                ),
            )
            self._emit_replay(snapshot, run_revision, receipt)
            return result
        current = next(
            (item for item in snapshot.edges if item.edge_id == edge_id), None
        )
        if current is not None:
            current_revision = next(
                item
                for item in snapshot.edge_revisions
                if item.edge_id == edge_id and item.revision == current.current_revision
            )
            if (
                current_revision.relation,
                current_revision.claim_revision,
                current_revision.evidence_revision,
            ) == (relation, claim_rev, evidence_rev):
                return EdgeView(edge=current, revision=current_revision)
        now = self._clock.now()
        revision_number = 1 if current is None else current.current_revision + 1
        revision = ClaimEvidenceEdgeRevision(
            run_id=run_id,
            edge_id=edge_id,
            revision=revision_number,
            relation=relation,
            claim_revision=claim_rev,
            evidence_revision=evidence_rev,
            created_at=now,
            supersedes_revision=None if current is None else current.current_revision,
        )
        edge = ClaimEvidenceEdge(
            run_id=run_id,
            edge_id=edge_id,
            claim_id=claim_id,
            evidence_id=evidence_id,
            current_revision=revision_number,
            created_at=now if current is None else current.created_at,
            updated_at=now,
        )
        mutation = ClaimMutationReceipt(
            receipt_id=stable_id("cmr", [op, request_hash]),
            run_id=run_id,
            operation_key=op,
            request_hash=request_hash,
            claim_id=claim_id,
            claim_revision=claim_rev,
            edge_id=edge_id,
            edge_revision=revision_number,
            recorded_at=now,
        )
        edges = tuple(item for item in snapshot.edges if item.edge_id != edge_id) + (
            edge,
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "edges": tuple(sorted(edges, key=lambda x: x.edge_id)),
                "edge_revisions": tuple(
                    sorted(
                        (*snapshot.edge_revisions, revision),
                        key=lambda x: (x.edge_id, x.revision),
                    )
                ),
                "receipts": tuple(
                    sorted((*snapshot.receipts, mutation), key=lambda x: x.receipt_id)
                ),
            }
        )
        event_type = (
            TraceEventType.CLAIM_RELATION_CREATED
            if current is None
            else TraceEventType.CLAIM_RELATION_REVISED
        )
        self._commit(
            updated,
            snapshot.store_revision,
            run_revision,
            event_type,
            edge_id,
            {
                "edge_id": edge_id,
                "claim_id": claim_id,
                "evidence_id": evidence_id,
                "edge_revision": revision_number,
                "relation": relation.value,
                "operation_key": op,
            },
        )
        return EdgeView(edge=edge, revision=revision)

    def tombstone_claim(
        self, *, run_id: str, run_revision: int, claim_id: str
    ) -> ClaimRecord:
        snapshot = self._store.load(run_id)
        current = next(
            (item for item in snapshot.claims if item.claim_id == claim_id), None
        )
        if current is None:
            raise ClaimGraphPreconditionError("claim does not exist")
        changed = current.model_copy(
            update={
                "lifecycle": RecordLifecycle.TOMBSTONED,
                "updated_at": self._clock.now(),
            }
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "claims": tuple(
                    changed if item.claim_id == claim_id else item
                    for item in snapshot.claims
                ),
            }
        )
        self._commit(
            updated,
            snapshot.store_revision,
            run_revision,
            TraceEventType.CLAIM_TOMBSTONED,
            claim_id,
            {"claim_id": claim_id, "claim_revision": changed.current_revision},
        )
        return changed

    def tombstone_edge(
        self, *, run_id: str, run_revision: int, edge_id: str
    ) -> ClaimEvidenceEdge:
        snapshot = self._store.load(run_id)
        current = next(
            (item for item in snapshot.edges if item.edge_id == edge_id), None
        )
        if current is None:
            raise ClaimGraphPreconditionError("edge does not exist")
        changed = current.model_copy(
            update={
                "lifecycle": RecordLifecycle.TOMBSTONED,
                "updated_at": self._clock.now(),
            }
        )
        updated = snapshot.model_copy(
            update={
                "store_revision": snapshot.store_revision + 1,
                "edges": tuple(
                    changed if item.edge_id == edge_id else item
                    for item in snapshot.edges
                ),
            }
        )
        self._commit(
            updated,
            snapshot.store_revision,
            run_revision,
            TraceEventType.CLAIM_RELATION_TOMBSTONED,
            edge_id,
            {"edge_id": edge_id, "edge_revision": changed.current_revision},
        )
        return changed

    def list_claims(
        self,
        run_id: str,
        *,
        after_id: str | None = None,
        limit: int = 50,
        include_tombstoned: bool = False,
    ) -> tuple[ClaimView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        snapshot = self._store.load(run_id)
        records = sorted(
            (
                item
                for item in snapshot.claims
                if after_id is None or item.claim_id > after_id
                if include_tombstoned or item.lifecycle is RecordLifecycle.ACTIVE
            ),
            key=lambda x: x.claim_id,
        )[:limit]
        return tuple(
            self._claim_view(snapshot, item.claim_id, item.current_revision)
            for item in records
        )

    def get_claim(
        self,
        run_id: str,
        claim_id: str,
        *,
        revision: int | None = None,
        include_tombstoned: bool = False,
    ) -> ClaimView:
        snapshot = self._store.load(run_id)
        record = next(
            (item for item in snapshot.claims if item.claim_id == claim_id), None
        )
        if record is None or (
            record.lifecycle is RecordLifecycle.TOMBSTONED and not include_tombstoned
        ):
            raise ClaimGraphPreconditionError("claim does not exist")
        selected = revision or record.current_revision
        revision_record = next(
            (
                item
                for item in snapshot.claim_revisions
                if item.claim_id == claim_id and item.revision == selected
            ),
            None,
        )
        if revision_record is None:
            raise ClaimGraphPreconditionError("claim revision does not exist")
        return ClaimView(claim=record, revision=revision_record)

    def get_edge(
        self,
        run_id: str,
        edge_id: str,
        *,
        revision: int | None = None,
        include_tombstoned: bool = False,
    ) -> EdgeView:
        snapshot = self._store.load(run_id)
        edge = next((item for item in snapshot.edges if item.edge_id == edge_id), None)
        if edge is None or (
            edge.lifecycle is RecordLifecycle.TOMBSTONED and not include_tombstoned
        ):
            raise ClaimGraphPreconditionError("edge does not exist")
        selected = revision or edge.current_revision
        edge_revision = next(
            (
                item
                for item in snapshot.edge_revisions
                if item.edge_id == edge_id and item.revision == selected
            ),
            None,
        )
        if edge_revision is None:
            raise ClaimGraphPreconditionError("edge revision does not exist")
        return EdgeView(edge=edge, revision=edge_revision)

    def evidence_for_claim(
        self,
        run_id: str,
        claim_id: str,
        *,
        relation: ClaimEvidenceRelation | None = None,
        after_id: str | None = None,
        limit: int = 50,
        current_only: bool = True,
        include_tombstoned: bool = False,
    ) -> tuple[EdgeView, ...]:
        self._validate_query_limit(limit)
        return tuple(
            item
            for item in self._query_edge_views(
                run_id,
                claim_id=claim_id,
                relation=relation,
                current_only=current_only,
                include_tombstoned=include_tombstoned,
            )
            if after_id is None or item.edge.edge_id > after_id
        )[:limit]

    def claims_for_evidence(
        self,
        run_id: str,
        evidence_id: str,
        *,
        relation: ClaimEvidenceRelation | None = None,
        after_id: str | None = None,
        limit: int = 50,
        current_only: bool = True,
        include_tombstoned: bool = False,
    ) -> tuple[EdgeView, ...]:
        self._validate_query_limit(limit)
        return tuple(
            item
            for item in self._query_edge_views(
                run_id,
                evidence_id=evidence_id,
                relation=relation,
                current_only=current_only,
                include_tombstoned=include_tombstoned,
            )
            if after_id is None or item.edge.edge_id > after_id
        )[:limit]

    def supporting_evidence(
        self, run_id: str, claim_id: str, **kwargs: object
    ) -> tuple[EdgeView, ...]:
        return self.evidence_for_claim(
            run_id,
            claim_id,
            relation=ClaimEvidenceRelation.SUPPORTS,
            **kwargs,
        )

    def contradicting_evidence(
        self, run_id: str, claim_id: str, **kwargs: object
    ) -> tuple[EdgeView, ...]:
        return self.evidence_for_claim(
            run_id,
            claim_id,
            relation=ClaimEvidenceRelation.CONTRADICTS,
            **kwargs,
        )

    def contextualizing_evidence(
        self, run_id: str, claim_id: str, **kwargs: object
    ) -> tuple[EdgeView, ...]:
        return self.evidence_for_claim(
            run_id,
            claim_id,
            relation=ClaimEvidenceRelation.CONTEXTUALIZES,
            **kwargs,
        )

    def conflict_candidates(self, run_id: str) -> tuple[ConflictCandidate, ...]:
        snapshot = self._store.load(run_id)
        results = []
        for claim in snapshot.claims:
            if claim.lifecycle is not RecordLifecycle.ACTIVE:
                continue
            current_edges = self._query_edge_views(
                run_id,
                claim_id=claim.claim_id,
                current_only=True,
                include_tombstoned=False,
            )
            supports = tuple(
                sorted(
                    item.edge.evidence_id
                    for item in current_edges
                    if item.revision.relation is ClaimEvidenceRelation.SUPPORTS
                )
            )
            contradicts = tuple(
                sorted(
                    item.edge.evidence_id
                    for item in current_edges
                    if item.revision.relation is ClaimEvidenceRelation.CONTRADICTS
                )
            )
            if supports and contradicts:
                results.append(
                    ConflictCandidate(
                        claim_id=claim.claim_id,
                        supporting_evidence_ids=supports,
                        contradicting_evidence_ids=contradicts,
                    )
                )
        return tuple(sorted(results, key=lambda x: x.claim_id))

    def _query_edge_views(
        self,
        run_id: str,
        *,
        claim_id: str | None = None,
        evidence_id: str | None = None,
        relation: ClaimEvidenceRelation | None = None,
        current_only: bool,
        include_tombstoned: bool,
    ) -> tuple[EdgeView, ...]:
        snapshot = self._store.load(run_id)
        evidence_snapshot = self._evidence_store.load(run_id)
        claim_by_id = {item.claim_id: item for item in snapshot.claims}
        evidence_by_id = {item.evidence_id: item for item in evidence_snapshot.evidence}
        views: list[EdgeView] = []
        for edge in snapshot.edges:
            if claim_id is not None and edge.claim_id != claim_id:
                continue
            if evidence_id is not None and edge.evidence_id != evidence_id:
                continue
            claim = claim_by_id[edge.claim_id]
            evidence = evidence_by_id.get(edge.evidence_id)
            if evidence is None:
                continue
            if not include_tombstoned and (
                edge.lifecycle is RecordLifecycle.TOMBSTONED
                or claim.lifecycle is RecordLifecycle.TOMBSTONED
                or evidence.lifecycle is RecordLifecycle.TOMBSTONED
            ):
                continue
            revisions = (
                (
                    next(
                        item
                        for item in snapshot.edge_revisions
                        if item.edge_id == edge.edge_id
                        and item.revision == edge.current_revision
                    ),
                )
                if current_only
                else tuple(
                    item
                    for item in snapshot.edge_revisions
                    if item.edge_id == edge.edge_id
                )
            )
            for edge_revision in revisions:
                if current_only and (
                    edge_revision.claim_revision != claim.current_revision
                    or edge_revision.evidence_revision != evidence.current_revision
                ):
                    continue
                if relation is not None and edge_revision.relation is not relation:
                    continue
                views.append(EdgeView(edge=edge, revision=edge_revision))
        return tuple(
            sorted(views, key=lambda item: (item.edge.edge_id, item.revision.revision))
        )

    @staticmethod
    def _validate_query_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    def duplicate_claims(self, run_id: str, claim_id: str) -> tuple[str, ...]:
        snapshot = self._store.load(run_id)
        target = self._claim_view(
            snapshot,
            claim_id,
            next(
                item for item in snapshot.claims if item.claim_id == claim_id
            ).current_revision,
        ).revision
        return tuple(
            sorted(
                item.claim_id
                for item in snapshot.claims
                if item.claim_id != claim_id
                and self._claim_view(
                    snapshot, item.claim_id, item.current_revision
                ).revision.normalized_statement_hash
                == target.normalized_statement_hash
            )
        )

    def _load_or_create(self, run_id: str) -> ClaimGraphSnapshot:
        try:
            return self._store.load(run_id)
        except ClaimGraphNotFound:
            initial = ClaimGraphSnapshot(run_id=run_id, store_revision=0)
            with suppress(ClaimGraphAlreadyExists):
                self._store.create(initial)
            return self._store.load(run_id)

    @staticmethod
    def _claim_view(
        snapshot: ClaimGraphSnapshot, claim_id: str, revision: int
    ) -> ClaimView:
        return ClaimView(
            claim=next(item for item in snapshot.claims if item.claim_id == claim_id),
            revision=next(
                item
                for item in snapshot.claim_revisions
                if item.claim_id == claim_id and item.revision == revision
            ),
        )

    def _commit(
        self,
        snapshot: ClaimGraphSnapshot,
        expected_revision: int,
        run_revision: int,
        event_type: TraceEventType,
        correlation_id: str,
        attributes: dict[str, object],
    ) -> None:
        self._redactor.assert_safe_model(snapshot)
        self._store.save(snapshot, expected_revision=expected_revision)
        try:
            self._trace.append(
                TraceEvent(
                    event_id=stable_id(
                        "evt",
                        [event_type.value, correlation_id, snapshot.store_revision],
                    ),
                    event_type=event_type,
                    timestamp=self._clock.now(),
                    run_id=snapshot.run_id,
                    revision=run_revision,
                    correlation_id=correlation_id,
                    attributes={
                        **attributes,
                        "store_revision": snapshot.store_revision,
                    },
                )
            )
        except Exception as exc:
            raise ClaimGraphTraceCommitError(
                "claim graph committed but trace append failed",
                run_id=snapshot.run_id,
                store_revision=snapshot.store_revision,
            ) from exc

    def _emit_replay(
        self,
        snapshot: ClaimGraphSnapshot,
        run_revision: int,
        receipt: ClaimMutationReceipt,
    ) -> None:
        try:
            self._trace.append(
                TraceEvent(
                    event_id=stable_id(
                        "evt",
                        [
                            "claim-mutation-replay",
                            receipt.receipt_id,
                            snapshot.store_revision,
                        ],
                    ),
                    event_type=TraceEventType.CLAIM_MUTATION_REPLAYED,
                    timestamp=self._clock.now(),
                    run_id=snapshot.run_id,
                    revision=run_revision,
                    correlation_id=receipt.receipt_id,
                    attributes={
                        "operation_key": receipt.operation_key,
                        "claim_id": receipt.claim_id,
                        "claim_revision": receipt.claim_revision,
                        "edge_id": receipt.edge_id,
                        "edge_revision": receipt.edge_revision,
                        "store_revision": snapshot.store_revision,
                    },
                )
            )
        except Exception as exc:
            raise ClaimGraphTraceCommitError(
                "claim mutation replay trace append failed",
                run_id=snapshot.run_id,
                store_revision=snapshot.store_revision,
            ) from exc
