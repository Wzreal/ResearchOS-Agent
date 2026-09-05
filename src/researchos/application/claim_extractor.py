"""Durable, bounded Claim Extraction over frozen Evidence authority."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from pydantic import ValidationError

from researchos.application.claim_extraction_operation import (
    ClaimExtractionOperationManager,
)
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.errors import (
    ClaimExtractionOperationNotFound,
    CorruptClaimExtractionOperation,
    PlanningModelFailure,
)
from researchos.domain.claim_extraction import (
    ClaimExtractionEvidenceItem,
    ClaimExtractionModelResult,
    ClaimExtractionPolicy,
    ClaimExtractionRequest,
    ClaimExtractionResponse,
)
from researchos.domain.claim_extraction_operation import (
    ClaimExtractionOperation,
    ClaimExtractionOperationStatus,
)
from researchos.domain.claims import ClaimGenerationContext
from researchos.domain.contracts import RunState, canonical_json_bytes, model_sha256
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.identity import stable_hash, stable_id
from researchos.interfaces.claim_extraction import (
    ClaimExtractionModel,
    ClaimExtractionOperationStore,
)
from researchos.interfaces.evidence import EvidenceStore
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.runtime import CancellationSignal
from researchos.security.redaction import PersistenceRedactor

IdFactory = Callable[[str], str]


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class ClaimExtractor:
    def __init__(
        self,
        *,
        operations: ClaimExtractionOperationStore,
        operation_manager: ClaimExtractionOperationManager,
        evidence_store: EvidenceStore,
        graph: ClaimGraphService,
        model: ClaimExtractionModel,
        clock: Clock,
        id_factory: IdFactory | None = None,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._operations = operations
        self._manager = operation_manager
        self._evidence = evidence_store
        self._graph = graph
        self._model = model
        self._clock = clock
        self._id_factory = id_factory or _default_id_factory
        self._redactor = redactor or PersistenceRedactor()

    def execute(
        self,
        state: RunState,
        *,
        extraction_id: str,
        policy: ClaimExtractionPolicy,
        workflow_budget_slice_hash: str,
        cancellation: CancellationSignal | None = None,
    ) -> ClaimExtractionOperation:
        request = self._request(state, extraction_id, policy)
        operation = self._load_or_prepare(
            state, request, workflow_budget_slice_hash=workflow_budget_slice_hash
        )
        if operation.status is ClaimExtractionOperationStatus.DISPATCHED:
            return self._manager.recover(operation)
        if operation.status is ClaimExtractionOperationStatus.PREPARED:
            if cancellation is not None and cancellation.cancelled:
                return self._manager.transition(
                    operation,
                    ClaimExtractionOperationStatus.CANCELLED,
                    failure_code="claim_extraction_cancelled",
                )
            operation = self._manager.transition(
                operation, ClaimExtractionOperationStatus.DISPATCHED
            )
            try:
                raw = self._model.generate(request)
                settlement = ClaimExtractionModelResult.model_validate(raw)
                response = settlement.response
                self._validate_response(request, response)
                self._redactor.assert_safe_model(response)
            except (ValidationError, ValueError):
                return self._manager.transition(
                    operation,
                    ClaimExtractionOperationStatus.FAILED,
                    failure_code="claim_extraction_invalid_response",
                )
            except PlanningModelFailure as exc:
                return self._manager.transition(
                    operation,
                    ClaimExtractionOperationStatus.FAILED,
                    usage=exc.usage,
                    usage_certainty=exc.usage_certainty,
                    failure_code="claim_extraction_model_failed",
                )
            except Exception:
                return self._manager.transition(
                    operation,
                    ClaimExtractionOperationStatus.FAILED,
                    failure_code="claim_extraction_model_failed",
                )
            operation = self._manager.transition(
                operation,
                ClaimExtractionOperationStatus.VALIDATED,
                response=response,
                usage=settlement.usage,
                usage_certainty=settlement.usage_certainty,
                mutation_keys=self._mutation_keys(operation, response),
            )
        if operation.status is ClaimExtractionOperationStatus.VALIDATED:
            self._replay(state, operation)
            return self._manager.transition(
                operation, ClaimExtractionOperationStatus.COMPLETED
            )
        return operation

    def _request(
        self, state: RunState, extraction_id: str, policy: ClaimExtractionPolicy
    ) -> ClaimExtractionRequest:
        snapshot = self._evidence.load(state.run_id)
        records = {item.evidence_id: item for item in snapshot.evidence}
        revisions = {
            (item.evidence_id, item.revision): item for item in snapshot.revisions
        }
        admitted: list[ClaimExtractionEvidenceItem] = []
        omitted: list[str] = []
        for evidence_id in sorted(records):
            record = records[evidence_id]
            if record.lifecycle is not RecordLifecycle.ACTIVE:
                continue
            revision = revisions.get((evidence_id, record.current_revision))
            if revision is None:
                raise CorruptClaimExtractionOperation("active evidence has no revision")
            item = ClaimExtractionEvidenceItem(
                evidence_id=evidence_id,
                revision=revision.revision,
                content=revision.content,
                content_hash=revision.content_hash,
            )
            if (
                len(admitted) >= policy.max_evidence_items
                or sum(value.content_bytes for value in admitted) + item.content_bytes
                > policy.max_context_bytes
            ):
                omitted.append(evidence_id)
            else:
                admitted.append(item)
        return ClaimExtractionRequest(
            request_id=self._id_factory("creq"),
            run_id=state.run_id,
            run_revision=state.revision,
            extraction_id=extraction_id,
            policy=policy,
            policy_hash=model_sha256(policy),
            evidence_snapshot_hash=model_sha256(snapshot),
            evidence_items=tuple(admitted),
            omitted_evidence_ids=tuple(omitted),
            requested_at=self._clock.now(),
        )

    def _load_or_prepare(
        self,
        state: RunState,
        request: ClaimExtractionRequest,
        *,
        workflow_budget_slice_hash: str,
    ) -> ClaimExtractionOperation:
        request_hash = stable_hash(
            {
                "run_id": request.run_id,
                "run_revision": request.run_revision,
                "extraction_id": request.extraction_id,
                "policy_hash": request.policy_hash,
                "evidence_snapshot_hash": request.evidence_snapshot_hash,
                "evidence": [
                    (item.evidence_id, item.revision, item.content_hash)
                    for item in request.evidence_items
                ],
                "omitted_evidence_ids": request.omitted_evidence_ids,
            }
        )
        try:
            operation = self._operations.load(state.run_id, request.extraction_id)
        except ClaimExtractionOperationNotFound:
            operation = ClaimExtractionOperation(
                operation_id=self._id_factory("ceop"),
                run_id=state.run_id,
                run_revision=state.revision,
                extraction_id=request.extraction_id,
                policy_hash=request.policy_hash,
                model_bundle_hash=self._model.model_bundle_hash,
                evidence_snapshot_hash=request.evidence_snapshot_hash,
                request_hash=request_hash,
                workflow_budget_slice_hash=workflow_budget_slice_hash,
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            return self._manager.create(operation)
        if (
            operation.run_revision != state.revision
            or operation.request_hash != request_hash
            or operation.evidence_snapshot_hash != request.evidence_snapshot_hash
            or operation.policy_hash != request.policy_hash
            or operation.model_bundle_hash != self._model.model_bundle_hash
            or operation.workflow_budget_slice_hash != workflow_budget_slice_hash
        ):
            raise CorruptClaimExtractionOperation("extraction operation pins differ")
        return operation

    @staticmethod
    def _validate_response(
        request: ClaimExtractionRequest, response: ClaimExtractionResponse
    ) -> None:
        if (
            response.run_id != request.run_id
            or response.extraction_id != request.extraction_id
        ):
            raise ValueError("claim extraction response identity differs")
        if len(response.candidates) > request.policy.max_claims:
            raise ValueError("claim count bound exceeded")
        if len(canonical_json_bytes(response)) > request.policy.max_response_bytes:
            raise ValueError("response byte bound exceeded")
        admitted = {
            (item.evidence_id, item.revision) for item in request.evidence_items
        }
        for candidate in response.candidates:
            if (
                len(candidate.statement.encode("utf-8"))
                > request.policy.max_statement_bytes
            ):
                raise ValueError("statement byte bound exceeded")
            if len(candidate.evidence_pins) > request.policy.max_references_per_claim:
                raise ValueError("reference bound exceeded")
            if any(
                (pin.evidence_id, pin.evidence_revision) not in admitted
                for pin in candidate.evidence_pins
            ):
                raise ValueError("response references evidence outside frozen snapshot")

    def _replay(self, state: RunState, operation: ClaimExtractionOperation) -> None:
        response = operation.validated_response
        if response is None:
            raise CorruptClaimExtractionOperation("validated extraction lacks response")
        mutation_keys = iter(operation.mutation_keys)
        for candidate in response.candidates:
            scope = stable_id("scope", [operation.extraction_id, candidate.ordinal])
            try:
                create_key = next(mutation_keys)
            except StopIteration as exc:
                raise CorruptClaimExtractionOperation(
                    "mutation keys are incomplete"
                ) from exc
            receipt = self._graph.mutation_receipt(state.run_id, create_key)
            if receipt is None:
                view = self._graph.create_claim(
                    run_id=state.run_id,
                    run_revision=state.revision,
                    claim_scope_key=scope,
                    statement=candidate.statement,
                    generation_context=ClaimGenerationContext(
                        run_revision=state.revision,
                        producer_id="claim_extraction",
                        source_operation_key=operation.request_hash,
                    ),
                    operation_key=create_key,
                )
                claim_id = view.claim.claim_id
                claim_revision = view.revision.revision
            else:
                claim_id = receipt.claim_id
                claim_revision = receipt.claim_revision
            for pin in candidate.evidence_pins:
                try:
                    edge_key = next(mutation_keys)
                except StopIteration as exc:
                    raise CorruptClaimExtractionOperation(
                        "mutation keys are incomplete"
                    ) from exc
                if self._graph.mutation_receipt(state.run_id, edge_key) is None:
                    self._graph.relate(
                        run_id=state.run_id,
                        run_revision=state.revision,
                        claim_id=claim_id,
                        claim_revision=claim_revision,
                        evidence_id=pin.evidence_id,
                        evidence_revision=pin.evidence_revision,
                        relation=pin.relation,
                        operation_key=edge_key,
                    )
        if next(mutation_keys, None) is not None:
            raise CorruptClaimExtractionOperation("mutation keys are excessive")

    @staticmethod
    def _mutation_keys(
        operation: ClaimExtractionOperation, response: ClaimExtractionResponse
    ) -> tuple[str, ...]:
        keys: list[str] = []
        for candidate in response.candidates:
            create_key = stable_hash(
                [
                    "claim-extraction-create-v1",
                    operation.operation_id,
                    candidate.ordinal,
                ]
            )
            keys.append(create_key)
            keys.extend(
                stable_hash(
                    [
                        "claim-extraction-edge-v1",
                        create_key,
                        pin.model_dump(mode="json"),
                    ]
                )
                for pin in candidate.evidence_pins
            )
        return tuple(keys)
