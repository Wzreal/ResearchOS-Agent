"""Public Phase 6 facade enforcing replay, lifecycle, and publication boundaries."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from researchos.adapters.claim_memory import InMemoryClaimGraphStore
from researchos.adapters.evidence_memory import InMemoryEvidenceStore
from researchos.application.citation_integrity import CitationIntegrityValidator
from researchos.application.errors import (
    VerificationArtifactConflict,
    VerificationCancelled,
    VerificationInputChanged,
    VerificationPreconditionError,
    VerificationTraceCommitError,
    VerificationUsageUncertain,
)
from researchos.application.verification_coordinator import (
    VerificationCoordinator,
    determine_disposition,
)
from researchos.application.verification_input import (
    compute_verification_id,
    freeze_verification_input,
)
from researchos.application.verification_publisher import render_markdown
from researchos.domain.claims import CitationSeverity
from researchos.domain.contracts import (
    RunState,
    RunStatus,
    TraceEvent,
    TraceEventType,
    canonical_json_bytes,
    model_sha256,
)
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import RuntimeResourceAmount
from researchos.domain.synthesis import (
    CitationAssignment,
    FrozenVerificationInput,
    JudgeVerdict,
    VerificationPolicy,
    VerificationResult,
)
from researchos.interfaces.evidence import ClaimGraphStore, EvidenceStore
from researchos.interfaces.lifecycle import Clock, TraceSink
from researchos.interfaces.runtime import CancellationSignal
from researchos.interfaces.verification import (
    VerificationArtifactStore,
    VerificationCallJournal,
)
from researchos.security.redaction import PersistenceRedactor


class VerificationService:
    def __init__(
        self,
        *,
        claim_store: ClaimGraphStore,
        evidence_store: EvidenceStore,
        artifact_store: VerificationArtifactStore,
        coordinator: VerificationCoordinator,
        clock: Clock,
        trace_sink: TraceSink,
    ) -> None:
        self._claims = claim_store
        self._evidence = evidence_store
        self._artifacts = artifact_store
        self._coordinator = coordinator
        self._clock = clock
        self._trace = trace_sink
        self._redactor = PersistenceRedactor()

    @property
    def model_bundle_hash(self) -> str:
        return self._coordinator.model_bundle_hash

    async def verify(
        self,
        *,
        run_state: RunState,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        cancellation: CancellationSignal,
        expected_prior_verification_id: str | None = None,
        frozen_input: FrozenVerificationInput | None = None,
        call_journal: VerificationCallJournal | None = None,
        event_sink: Callable[[TraceEvent], None] | None = None,
    ) -> VerificationResult:
        if run_state.status is not RunStatus.VERIFYING:
            raise VerificationPreconditionError("run must already be VERIFYING")

        # Locked replay order: freeze, hash (inside freeze), identity, lookup.
        frozen = frozen_input or freeze_verification_input(
            run_id=run_state.run_id,
            run_revision=run_state.revision,
            claim_store=self._claims,
            evidence_store=self._evidence,
            policy=policy,
        )
        if (
            frozen.run_id != run_state.run_id
            or frozen.run_revision != run_state.revision
        ):
            raise VerificationPreconditionError(
                "prepared verification input belongs to another Run revision"
            )
        verification_id = compute_verification_id(
            frozen, policy, self._coordinator.model_bundle_hash
        )
        def emit(event_type: TraceEventType, attributes: dict[str, object]) -> None:
            self._emit(
                run_state,
                event_type,
                verification_id,
                attributes,
                event_sink=event_sink,
            )
        existing = self._artifacts.load(run_state.run_id)
        if existing is not None and existing.verification_id == verification_id:
            reconciled = self._artifacts.reconcile_report(run_state.run_id)
            emit(
                TraceEventType.VERIFICATION_PUBLICATION_REPLAYED,
                {"report_reconciled": reconciled},
            )
            return existing
        if (
            existing is not None
            and existing.verification_id != expected_prior_verification_id
        ):
            raise VerificationArtifactConflict(
                "expected prior verification identity differs"
            )
        if existing is None and expected_prior_verification_id is not None:
            raise VerificationArtifactConflict(
                "expected prior verification artifact is absent"
            )

        emit(
            TraceEventType.VERIFICATION_STARTED,
            {
                "frozen_input_hash": frozen.frozen_input_hash,
                "selected_claim_count": len(frozen.selected_claim_ids),
                "selected_evidence_count": len(frozen.selected_evidence_ids),
            },
        )
        try:
            (
                draft,
                draft_revisions,
                findings,
                resolved_finding_ids,
                acquisitions,
                decisions,
                usage,
                certainty,
                bounds_exhausted,
                round_records,
                termination_reason,
            ) = await self._coordinator.execute(
                verification_id=verification_id,
                frozen=frozen,
                policy=policy,
                hard_limits=hard_limits,
                deadline=run_state.config.deadline,
                cancellation=cancellation,
                expected_mode=run_state.config.mode,
                event_callback=lambda event_type, attributes: self._emit(
                    run_state,
                    event_type,
                    verification_id,
                    attributes,
                    event_sink=event_sink,
                ),
                call_journal=call_journal,
            )
        except VerificationCancelled:
            emit(
                TraceEventType.VERIFICATION_CANCELLED,
                {"failure_code": "verification_cancelled"},
            )
            raise
        except VerificationUsageUncertain:
            # The durable call journal already emitted the precise terminal
            # interrupted-unknown event. Do not mislabel it as known failure.
            raise
        except Exception as exc:
            emit(
                TraceEventType.VERIFICATION_FAILED,
                {"failure_code": type(exc).__name__},
            )
            raise
        claim_snapshot_store = InMemoryClaimGraphStore()
        evidence_snapshot_store = InMemoryEvidenceStore()
        claim_snapshot_store.create(frozen.claim_snapshot)
        evidence_snapshot_store.create(frozen.evidence_snapshot)
        citation_result = CitationIntegrityValidator(
            claim_store=claim_snapshot_store,
            evidence_store=evidence_snapshot_store,
        ).validate(run_state.run_id, draft.citations)
        claim_revision, claim_hash = self._snapshot_fingerprint(
            self._claims, run_state.run_id
        )
        evidence_revision, evidence_hash = self._snapshot_fingerprint(
            self._evidence, run_state.run_id
        )
        if (
            claim_revision != frozen.claim_snapshot.store_revision
            or claim_hash != frozen.claim_snapshot_hash
            or evidence_revision != frozen.evidence_snapshot.store_revision
            or evidence_hash != frozen.evidence_snapshot_hash
        ):
            emit(
                TraceEventType.VERIFICATION_FAILED,
                {"failure_code": "verification_input_changed"},
            )
            raise VerificationInputChanged("verification_input_changed")
        citation_errors = any(
            item.severity is CitationSeverity.ERROR for item in citation_result.issues
        )
        disposition = determine_disposition(
            draft,
            decisions,
            findings=findings,
            resolved_finding_ids=resolved_finding_ids,
            citation_issues=citation_result.issues,
            omitted=bool(
                frozen.omitted_claim_ids
                or frozen.omitted_evidence_ids
                or bounds_exhausted
            ),
            open_findings=any(
                item.verdict is JudgeVerdict.UNRESOLVED for item in decisions
            ),
            pending_acquisition=bool(acquisitions),
            citation_errors=citation_errors,
        )
        markdown = render_markdown(
            draft,
            decisions,
            findings=findings,
            resolved_finding_ids=resolved_finding_ids,
            citation_issues=citation_result.issues,
            disposition=disposition,
            acquisition_requests=acquisitions,
            omitted_claim_ids=frozen.omitted_claim_ids,
        )
        if len(markdown) > policy.max_report_bytes:
            emit(
                TraceEventType.VERIFICATION_FAILED,
                {"failure_code": "report_bound_exceeded"},
            )
            raise VerificationPreconditionError("report exceeds configured byte limit")
        report_claims_by_claim = {item.claim_id: item for item in draft.report_claims}
        current_edges = {
            (item.edge.claim_id, item.edge.evidence_id): item
            for item in frozen.current_valid_edges
        }
        citation_assignments = tuple(
            CitationAssignment(
                report_claim_id=report_claims_by_claim[item.claim_id].report_claim_id,
                citation=item,
                edge_id=current_edges[(item.claim_id, item.evidence_id)].edge.edge_id,
                relation=current_edges[
                    (item.claim_id, item.evidence_id)
                ].revision.relation,
            )
            for item in sorted(draft.citations, key=lambda item: item.citation_id)
        )
        result_data = dict(
            verification_id=verification_id,
            synthesis_id=draft.synthesis_id,
            run_id=run_state.run_id,
            run_revision=run_state.revision,
            frozen_input_hash=frozen.frozen_input_hash,
            claim_snapshot_hash=frozen.claim_snapshot_hash,
            evidence_snapshot_hash=frozen.evidence_snapshot_hash,
            claim_store_revision=frozen.claim_snapshot.store_revision,
            evidence_store_revision=frozen.evidence_snapshot.store_revision,
            selected_claim_ids=frozen.selected_claim_ids,
            omitted_claim_ids=frozen.omitted_claim_ids,
            selected_evidence_ids=frozen.selected_evidence_ids,
            omitted_evidence_ids=frozen.omitted_evidence_ids,
            policy_hash=model_sha256(policy),
            model_bundle_hash=self._coordinator.model_bundle_hash,
            supersedes_verification_id=(
                None if existing is None else existing.verification_id
            ),
            final_draft=draft,
            draft_revisions=draft_revisions,
            final_draft_revision_id=draft.draft_revision_id,
            findings=findings,
            resolved_finding_ids=resolved_finding_ids,
            acquisition_requests=acquisitions,
            judge_decisions=decisions,
            citation_assignments=citation_assignments,
            round_records=round_records,
            citation_issues=citation_result.issues,
            disposition=disposition,
            termination_reason=termination_reason,
            markdown_sha256=hashlib.sha256(markdown).hexdigest(),
            usage=usage,
            usage_certainty=certainty,
            completed_at=self._clock.now(),
        )
        provisional = VerificationResult.model_construct(
            **result_data, artifact_content_hash="0" * 64
        )
        result = VerificationResult(
            **result_data,
            artifact_content_hash=stable_hash(
                provisional.model_dump(mode="json", exclude={"artifact_content_hash"})
            ),
        )
        self._redactor.assert_safe_model(result)
        if len(canonical_json_bytes(result)) > policy.max_verification_artifact_bytes:
            emit(
                TraceEventType.VERIFICATION_FAILED,
                {"failure_code": "artifact_bound_exceeded"},
            )
            raise VerificationPreconditionError(
                "verification artifact exceeds byte limit"
            )
        if call_journal is not None:
            # Phase 8 persists only the publication boundary here; all result
            # construction and correctness remain owned by this Phase 6 service.
            call_journal.mark_ready_to_publish(
                result,
                markdown,
                expected_prior_verification_id=expected_prior_verification_id,
            )
        try:
            self._artifacts.publish(
                result,
                markdown,
                expected_prior_verification_id=expected_prior_verification_id,
            )
        except Exception as exc:
            emit(
                TraceEventType.VERIFICATION_FAILED,
                {"failure_code": type(exc).__name__},
            )
            raise
        try:
            emit(
                TraceEventType.VERIFICATION_COMPLETED,
                {
                    "disposition": disposition.value,
                    "draft_revision_id": draft.draft_revision_id,
                    "markdown_sha256": result.markdown_sha256,
                    "finding_count": len(findings),
                    "usage_certainty": certainty.value,
                },
            )
        except Exception as exc:
            raise VerificationTraceCommitError(
                "verification completion trace append failed",
                run_id=run_state.run_id,
                verification_id=verification_id,
            ) from exc
        return result

    def _emit(
        self,
        state: RunState,
        event_type: TraceEventType,
        verification_id: str,
        attributes: dict[str, object],
        *,
        event_sink: Callable[[TraceEvent], None] | None = None,
    ) -> None:
        event = TraceEvent(
            event_id=stable_id(
                "evt",
                [verification_id, event_type.value, state.revision, attributes],
            ),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=state.run_id,
            revision=state.revision,
            correlation_id=verification_id,
            attributes={"verification_id": verification_id, **attributes},
        )
        self._redactor.assert_safe_model(event)
        (event_sink or self._trace.append)(event)

    def prepare_invocation(
        self, *, run_state: RunState, policy: VerificationPolicy
    ) -> tuple[FrozenVerificationInput, str]:
        if run_state.status is not RunStatus.VERIFYING:
            raise VerificationPreconditionError("run must already be VERIFYING")
        frozen = freeze_verification_input(
            run_id=run_state.run_id,
            run_revision=run_state.revision,
            claim_store=self._claims,
            evidence_store=self._evidence,
            policy=policy,
        )
        return frozen, compute_verification_id(
            frozen, policy, self._coordinator.model_bundle_hash
        )

    def freeze_input(
        self, *, run_id: str, run_revision: int, policy: VerificationPolicy
    ) -> FrozenVerificationInput:
        return freeze_verification_input(
            run_id=run_id,
            run_revision=run_revision,
            claim_store=self._claims,
            evidence_store=self._evidence,
            policy=policy,
        )

    @staticmethod
    def _snapshot_fingerprint(store: object, run_id: str) -> tuple[int, str]:
        method = getattr(store, "snapshot_fingerprint", None)
        if method is None:
            raise VerificationPreconditionError(
                "store lacks snapshot fingerprint boundary"
            )
        revision, snapshot_hash = method(run_id)
        return int(revision), str(snapshot_hash)
