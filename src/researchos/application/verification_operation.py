"""Phase 8 verification-operation CAS, call journal, and outbox manager."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from researchos.application.errors import (
    CorruptVerificationOperation,
    RecoveryAttemptLimitExceeded,
    UnsafePersistenceData,
    VerificationContractError,
    VerificationModelFailure,
    VerificationOperationRevisionConflict,
    VerificationRecoveryInconsistency,
    VerificationUsageUncertain,
)
from researchos.application.observation_recorder import ObservationRecorder
from researchos.application.verification_publisher import render_markdown
from researchos.domain.contracts import TraceEvent, TraceEventType, model_sha256
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.observability import ObservationScope
from researchos.domain.runtime import (
    RuntimeResourceAmount,
    TraceEventDescriptor,
    UsageCertainty,
)
from researchos.domain.synthesis import (
    FrozenVerificationInput,
    VerificationModelRequest,
    VerificationModelResponse,
    VerificationPolicy,
    VerificationResult,
)
from researchos.domain.verification_runtime import (
    DEFAULT_RESPONSE_CONTRACT_VERSION,
    MAX_RECOVERY_ATTEMPTS,
    RESERVED_CRITICAL_SLOTS,
    TOTAL_DESCRIPTOR_SLOTS,
    CommittedModelResponse,
    ModelCallRecord,
    ModelCallStatus,
    PreparedModelCall,
    VerificationLineage,
    VerificationOperation,
    VerificationOperationStatus,
    VerificationOutboxItem,
    VerificationPublicationCandidate,
    max_model_calls,
    validate_operation_capacity,
)
from researchos.interfaces.lifecycle import Clock
from researchos.interfaces.verification import VerificationOperationStore
from researchos.security.redaction import PersistenceRedactor

LEGAL_OPERATION_TRANSITIONS: dict[
    VerificationOperationStatus, frozenset[VerificationOperationStatus]
] = {
    VerificationOperationStatus.INITIALIZED: frozenset(
        {
            VerificationOperationStatus.EXECUTING,
            VerificationOperationStatus.READY_TO_PUBLISH,
            VerificationOperationStatus.CANCELLED,
            VerificationOperationStatus.FAILED,
            VerificationOperationStatus.INTERRUPTED_UNKNOWN,
        }
    ),
    VerificationOperationStatus.EXECUTING: frozenset(
        {
            VerificationOperationStatus.READY_TO_PUBLISH,
            VerificationOperationStatus.CANCELLED,
            VerificationOperationStatus.FAILED,
            VerificationOperationStatus.INTERRUPTED_UNKNOWN,
        }
    ),
    VerificationOperationStatus.READY_TO_PUBLISH: frozenset(
        {
            VerificationOperationStatus.AUTHORITY_COMMITTED,
            VerificationOperationStatus.CANCELLED,
            VerificationOperationStatus.FAILED,
        }
    ),
    VerificationOperationStatus.AUTHORITY_COMMITTED: frozenset(
        {VerificationOperationStatus.LIFECYCLE_COMMITTED}
    ),
    VerificationOperationStatus.LIFECYCLE_COMMITTED: frozenset(
        {VerificationOperationStatus.COMPLETED}
    ),
    VerificationOperationStatus.COMPLETED: frozenset(),
    VerificationOperationStatus.FAILED: frozenset(),
    VerificationOperationStatus.CANCELLED: frozenset(
        {VerificationOperationStatus.AUTHORITY_COMMITTED}
    ),
    VerificationOperationStatus.INTERRUPTED_UNKNOWN: frozenset(
        {VerificationOperationStatus.AUTHORITY_COMMITTED}
    ),
}

LEGAL_MODEL_CALL_TRANSITIONS: dict[ModelCallStatus, frozenset[ModelCallStatus]] = {
    ModelCallStatus.PREPARED: frozenset({ModelCallStatus.DISPATCHED}),
    ModelCallStatus.DISPATCHED: frozenset(
        {
            ModelCallStatus.RESPONSE_COMMITTED,
            ModelCallStatus.FAILED,
            ModelCallStatus.TIMED_OUT,
            ModelCallStatus.CANCELLED,
            ModelCallStatus.INTERRUPTED_UNKNOWN,
        }
    ),
    ModelCallStatus.RESPONSE_COMMITTED: frozenset(),
    ModelCallStatus.FAILED: frozenset(),
    ModelCallStatus.TIMED_OUT: frozenset(),
    ModelCallStatus.CANCELLED: frozenset(),
    ModelCallStatus.INTERRUPTED_UNKNOWN: frozenset(),
}


def validate_operation_transition(
    current: VerificationOperationStatus, target: VerificationOperationStatus
) -> None:
    if target not in LEGAL_OPERATION_TRANSITIONS[current]:
        raise VerificationContractError(
            f"illegal verification operation transition {current} -> {target}"
        )


def validate_model_call_transition(
    current: ModelCallStatus, target: ModelCallStatus
) -> None:
    if target not in LEGAL_MODEL_CALL_TRANSITIONS[current]:
        raise VerificationContractError(
            f"illegal verification model-call transition {current} -> {target}"
        )


@dataclass(frozen=True, slots=True)
class VerificationOperationSession:
    manager: VerificationOperationManager
    run_id: str
    verification_id: str | None = None

    def prepare(
        self,
        request: VerificationModelRequest,
        *,
        response_contract_version: str = DEFAULT_RESPONSE_CONTRACT_VERSION,
    ) -> tuple[PreparedModelCall, CommittedModelResponse | None]:
        return self.manager.prepare_call(
            self.run_id,
            request,
            verification_id=self.verification_id,
            response_contract_version=response_contract_version,
        )

    def mark_dispatched(self, prepared: PreparedModelCall) -> None:
        self.manager.mark_dispatched(
            self.run_id, prepared, verification_id=self.verification_id
        )

    def commit_response(
        self,
        prepared: PreparedModelCall,
        *,
        validated_payload: dict[str, Any],
        response: VerificationModelResponse,
    ) -> CommittedModelResponse:
        return self.manager.commit_response(
            self.run_id,
            prepared,
            verification_id=self.verification_id,
            validated_payload=validated_payload,
            response=response,
        )

    def mark_terminal(
        self,
        prepared: PreparedModelCall,
        *,
        status: ModelCallStatus,
        failure_code: str,
        usage_certainty: UsageCertainty,
        usage: RuntimeResourceAmount | None = None,
    ) -> None:
        self.manager.mark_call_terminal(
            self.run_id,
            prepared,
            verification_id=self.verification_id,
            status=status,
            failure_code=failure_code,
            usage_certainty=usage_certainty,
            usage=usage,
        )

    def mark_ready_to_publish(
        self,
        result: VerificationResult,
        markdown: bytes,
        *,
        expected_prior_verification_id: str | None,
    ) -> None:
        self.manager.mark_ready_to_publish(
            self.run_id,
            result,
            markdown,
            expected_prior_verification_id=expected_prior_verification_id,
            verification_id=self.verification_id,
        )


class VerificationOperationManager:
    def __init__(
        self,
        *,
        store: VerificationOperationStore,
        clock: Clock,
        recorder: ObservationRecorder | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._recorder = recorder
        self._redactor = PersistenceRedactor()

    def create(
        self,
        *,
        frozen: FrozenVerificationInput,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        model_bundle_hash: str,
        verification_id: str,
        expected_prior_verification_id: str | None = None,
    ) -> VerificationOperation:
        validate_operation_capacity(policy)
        now = self._clock.now()
        operation = VerificationOperation(
            operation_id=stable_id(
                "vop", ["verification-operation-v1", verification_id]
            ),
            verification_id=verification_id,
            checkpoint_revision=0,
            run_id=frozen.run_id,
            run_revision=frozen.run_revision,
            status=VerificationOperationStatus.INITIALIZED,
            frozen_input_hash=frozen.frozen_input_hash,
            claim_snapshot_hash=frozen.claim_snapshot_hash,
            evidence_snapshot_hash=frozen.evidence_snapshot_hash,
            verification_policy=policy,
            policy_hash=model_sha256(policy),
            model_bundle_hash=model_bundle_hash,
            hard_limits=hard_limits,
            hard_limits_hash=model_sha256(hard_limits),
            expected_prior_verification_id=expected_prior_verification_id,
            created_at=now,
            updated_at=now,
        )
        descriptor = self._event(
            operation,
            TraceEventType.VERIFICATION_OPERATION_INITIALIZED,
            {"operation_id": operation.operation_id},
        )
        raw = operation.model_dump(mode="python")
        raw["outbox"] = (VerificationOutboxItem(descriptor=descriptor),)
        operation = VerificationOperation.model_validate(raw)
        self._store.create(operation)
        with suppress(Exception):
            self._deliver(
                operation.run_id, operation.verification_id, descriptor.event_id
            )
        return self._store.load(operation.run_id, operation.verification_id)

    def session(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperationSession:
        self._store.load(run_id, verification_id)
        return VerificationOperationSession(self, run_id, verification_id)

    def load(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        return self._store.load(run_id, verification_id)

    def list_generations(self, run_id: str) -> tuple[VerificationOperation, ...]:
        return self._store.list_for_run(run_id)

    def terminalize_predispatch(
        self,
        run_id: str,
        *,
        verification_id: str,
        status: VerificationOperationStatus,
        failure_code: str,
    ) -> VerificationOperation:
        if status not in {
            VerificationOperationStatus.FAILED,
            VerificationOperationStatus.CANCELLED,
        }:
            raise VerificationContractError(
                "predispatch terminal status must be FAILED or CANCELLED"
            )
        operation = self._store.load(run_id, verification_id)
        if operation.status in {
            VerificationOperationStatus.FAILED,
            VerificationOperationStatus.CANCELLED,
            VerificationOperationStatus.INTERRUPTED_UNKNOWN,
            VerificationOperationStatus.COMPLETED,
        }:
            return operation
        if any(
            item.status
            not in {ModelCallStatus.PREPARED, ModelCallStatus.RESPONSE_COMMITTED}
            for item in operation.model_calls
        ):
            raise VerificationRecoveryInconsistency(
                "predispatch failure cannot replace a dispatched outcome"
            )
        descriptor = self._event(
            operation,
            (
                TraceEventType.VERIFICATION_CANCELLED
                if status is VerificationOperationStatus.CANCELLED
                else TraceEventType.VERIFICATION_FAILED
            ),
            {"failure_code": failure_code, "dispatch_state": "not_dispatched"},
        )
        return self._save_update(
            operation,
            {"status": status, "last_error_code": failure_code},
            descriptors=(descriptor,),
        )

    def begin_recovery(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if operation.status is VerificationOperationStatus.COMPLETED:
            return operation
        if operation.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
            raise RecoveryAttemptLimitExceeded(
                "verification recovery attempt limit exceeded",
                operation_id=operation.operation_id,
                run_id=operation.run_id,
                current_attempts=operation.recovery_attempts,
                limit=MAX_RECOVERY_ATTEMPTS,
            )
        attempt = operation.recovery_attempts + 1
        calls = tuple(
            item.model_copy(
                update={
                    "status": ModelCallStatus.INTERRUPTED_UNKNOWN,
                    "terminal_at": self._clock.now(),
                    "failure_code": "interrupted_unknown",
                    "usage_certainty": UsageCertainty.UNKNOWN,
                }
            )
            if item.status is ModelCallStatus.DISPATCHED
            else item
            for item in operation.model_calls
        )
        target_status = (
            VerificationOperationStatus.INTERRUPTED_UNKNOWN
            if any(
                old.status is ModelCallStatus.DISPATCHED
                for old in operation.model_calls
            )
            else operation.status
        )
        started = self._event(
            operation,
            TraceEventType.VERIFICATION_OPERATION_RECOVERY_STARTED,
            {"recovery_attempt": attempt},
        )
        try:
            return self._save_update(
                operation,
                {
                    "recovery_attempts": attempt,
                    "model_calls": calls,
                    "status": target_status,
                },
                descriptors=(started,),
            )
        except VerificationOperationRevisionConflict:
            current = self._store.load(run_id, verification_id)
            if current.recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
                raise RecoveryAttemptLimitExceeded(
                    "verification recovery attempt limit exceeded",
                    operation_id=current.operation_id,
                    run_id=current.run_id,
                    current_attempts=current.recovery_attempts,
                    limit=MAX_RECOVERY_ATTEMPTS,
                ) from None
            raise

    def complete_recovery(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if operation.recovery_attempts == 0:
            raise VerificationRecoveryInconsistency(
                "recovery completion requires a started attempt"
            )
        attempt = operation.recovery_attempts
        if any(
            item.descriptor.event_type
            is TraceEventType.VERIFICATION_OPERATION_RECOVERY_COMPLETED
            and item.descriptor.attributes.get("recovery_attempt") == attempt
            for item in operation.outbox
        ):
            return operation
        completed = self._event(
            operation,
            TraceEventType.VERIFICATION_OPERATION_RECOVERY_COMPLETED,
            {
                "recovery_attempt": attempt,
                "operation_status": operation.status.value,
            },
        )
        return self._save_update(operation, {}, descriptors=(completed,))

    def reconcile_outbox(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        for item in operation.outbox:
            if not item.delivered:
                with suppress(Exception):
                    self._deliver(
                        run_id,
                        operation.verification_id,
                        item.descriptor.event_id,
                    )
                operation = self._store.load(run_id, operation.verification_id)
        return operation

    def record_event(
        self,
        run_id: str,
        event: TraceEvent,
        *,
        verification_id: str | None = None,
        critical: bool = False,
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if event.run_id != operation.run_id or event.revision != operation.run_revision:
            raise VerificationRecoveryInconsistency(
                "observation event differs from verification operation Run pin"
            )
        descriptor = TraceEventDescriptor.from_event(event)
        existing = next(
            (
                item
                for item in operation.outbox
                if item.descriptor.event_id == descriptor.event_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.descriptor.canonical_event_hash
                != descriptor.canonical_event_hash
            ):
                raise VerificationRecoveryInconsistency(
                    "observation identity has different content"
                )
            with suppress(Exception):
                self._deliver(
                    run_id, operation.verification_id, descriptor.event_id
                )
            return self._store.load(run_id, operation.verification_id)
        optional_count = sum(not item.critical for item in operation.outbox)
        optional_limit = TOTAL_DESCRIPTOR_SLOTS - RESERVED_CRITICAL_SLOTS
        if not critical and optional_count >= optional_limit - 1:
            prior = operation.optional_omitted_hash or ("0" * 64)
            return self._save_update(
                operation,
                {
                    "optional_omitted_count": operation.optional_omitted_count + 1,
                    "optional_omitted_hash": stable_hash(
                        [prior, descriptor.canonical_event_hash]
                    ),
                },
            )
        return self._save_update(
            operation,
            {},
            descriptors=(descriptor,),
            descriptors_critical=critical,
        )

    def flush_optional_omission_summary(
        self, run_id: str, verification_id: str | None = None
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if not operation.optional_omitted_count:
            return operation
        if any(
            item.descriptor.attributes.get("omission_summary") is True
            for item in operation.outbox
        ):
            return operation
        descriptor = self._event(
            operation,
            TraceEventType.OBSERVABILITY_OPTIONAL_OMITTED,
            {
                "omission_summary": True,
                "omitted_count": operation.optional_omitted_count,
                "omitted_hash": operation.optional_omitted_hash,
                "export": False,
            },
        )
        return self._save_update(
            operation,
            {},
            descriptors=(descriptor,),
            descriptors_critical=False,
        )

    def record_lineage(
        self,
        run_id: str,
        lineage: VerificationLineage,
        *,
        verification_id: str | None = None,
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if lineage.verification_id != operation.verification_id:
            raise VerificationRecoveryInconsistency(
                "lineage belongs to another verification"
            )
        descriptor = self._event(
            operation,
            TraceEventType.VERIFICATION_LINEAGE_RECONSTRUCTED,
            {
                "lineage_hash": lineage.lineage_hash,
                "citation_count": len(lineage.citation_ids),
                "edge_count": len(lineage.current_edge_ids),
                "receipt_count": len(lineage.evidence_receipt_ids),
            },
        )
        return self._save_update(
            operation,
            {"lineage": lineage},
            descriptors=(descriptor,),
        )

    def prepare_call(
        self,
        run_id: str,
        request: VerificationModelRequest,
        *,
        verification_id: str | None = None,
        response_contract_version: str,
    ) -> tuple[PreparedModelCall, CommittedModelResponse | None]:
        operation = self._store.load(run_id, verification_id)
        self._redactor.assert_safe_model(request)
        if request.verification_id != operation.verification_id:
            raise VerificationRecoveryInconsistency(
                "model request belongs to another verification"
            )
        canonical_request_hash = model_sha256(request)
        stage_key = stable_id(
            "vstage",
            [
                request.verification_id,
                request.role.value,
                request.round_number,
                request.draft_revision_id,
            ],
        )
        existing_index = next(
            (
                index
                for index, item in enumerate(operation.model_calls)
                if item.prepared.stage_key == stage_key
            ),
            None,
        )
        predecessor_records = (
            operation.model_calls
            if existing_index is None
            else operation.model_calls[:existing_index]
        )
        predecessor_hashes = tuple(
            item.response.canonical_payload_hash
            for item in predecessor_records
            if item.response is not None
        )
        prepared = PreparedModelCall(
            response_contract_version=response_contract_version,
            stage_key=stage_key,
            model_call_key=stable_id(
                "vcall",
                [
                    stage_key,
                    operation.model_bundle_hash,
                    canonical_request_hash,
                    "verification-model-request-v1",
                    response_contract_version,
                    predecessor_hashes,
                ],
            ),
            verification_id=request.verification_id,
            model_bundle_hash=operation.model_bundle_hash,
            role=request.role,
            round_number=request.round_number,
            draft_revision_id=request.draft_revision_id,
            canonical_request_hash=canonical_request_hash,
            safe_request_descriptor={
                "verification_id": request.verification_id,
                "role": request.role.value,
                "round_number": request.round_number,
                "draft_revision_id": request.draft_revision_id,
                "frozen_input_hash": request.context.get("frozen_input_hash"),
            },
            predecessor_response_hashes=predecessor_hashes,
        )
        existing = (
            None
            if existing_index is None
            else operation.model_calls[existing_index]
        )
        if existing is not None:
            if existing.prepared != prepared:
                raise VerificationRecoveryInconsistency(
                    "reconstructed model request proof differs"
                )
            if existing.status is ModelCallStatus.RESPONSE_COMMITTED:
                assert existing.response is not None
                return prepared, existing.response
            if existing.status is ModelCallStatus.PREPARED:
                return prepared, None
            if existing.status in {
                ModelCallStatus.DISPATCHED,
                ModelCallStatus.INTERRUPTED_UNKNOWN,
            }:
                raise VerificationUsageUncertain(
                    "dispatched verification model outcome is unknown"
                )
            raise VerificationModelFailure(
                "verification model call is durably terminal"
            )
        if len(operation.model_calls) >= max_model_calls(
            operation.verification_policy
        ):
            raise VerificationContractError("verification model call bound exceeded")
        if operation.status is VerificationOperationStatus.INITIALIZED:
            operation = self._save_update(
                operation, {"status": VerificationOperationStatus.EXECUTING}
            )
        record = ModelCallRecord(prepared=prepared, status=ModelCallStatus.PREPARED)
        descriptor = self._event(
            operation,
            TraceEventType.VERIFICATION_MODEL_CALL_PREPARED,
            {
                "stage_key": prepared.stage_key,
                "model_call_key": prepared.model_call_key,
            },
        )
        self._save_update(
            operation,
            {"model_calls": (*operation.model_calls, record)},
            descriptors=(descriptor,),
        )
        return prepared, None

    def mark_dispatched(
        self,
        run_id: str,
        prepared: PreparedModelCall,
        *,
        verification_id: str | None = None,
    ) -> None:
        operation = self._store.load(run_id, verification_id)
        calls, current = self._replace_call(operation, prepared.model_call_key)
        if current.status is not ModelCallStatus.PREPARED:
            raise VerificationUsageUncertain(
                "verification model call was already dispatched"
            )
        validate_model_call_transition(current.status, ModelCallStatus.DISPATCHED)
        dispatched = current.model_copy(
            update={
                "status": ModelCallStatus.DISPATCHED,
                "dispatched_at": self._clock.now(),
            }
        )
        calls[current.prepared.model_call_key] = dispatched
        descriptor = self._event(
            operation,
            TraceEventType.VERIFICATION_MODEL_CALL_DISPATCHED,
            {
                "stage_key": prepared.stage_key,
                "model_call_key": prepared.model_call_key,
            },
        )
        self._save_update(
            operation,
            {"model_calls": self._ordered_calls(operation, calls)},
            descriptors=(descriptor,),
        )

    def commit_response(
        self,
        run_id: str,
        prepared: PreparedModelCall,
        *,
        verification_id: str | None = None,
        validated_payload: dict[str, Any],
        response: VerificationModelResponse,
    ) -> CommittedModelResponse:
        operation = self._store.load(run_id, verification_id)
        if self._redactor.redact_value(validated_payload) != validated_payload:
            raise UnsafePersistenceData(
                "validated model payload requires persistence redaction"
            )
        calls, current = self._replace_call(operation, prepared.model_call_key)
        if current.status is ModelCallStatus.RESPONSE_COMMITTED:
            assert current.response is not None
            candidate_hash = stable_hash(validated_payload)
            if current.response.canonical_payload_hash != candidate_hash:
                raise VerificationRecoveryInconsistency(
                    "same committed model call has different response"
                )
            return current.response
        if current.status is not ModelCallStatus.DISPATCHED:
            raise VerificationContractError(
                "model response can commit only after dispatch"
            )
        validate_model_call_transition(
            current.status, ModelCallStatus.RESPONSE_COMMITTED
        )
        committed = CommittedModelResponse(
            model_call_key=prepared.model_call_key,
            role=response.role,
            round_number=response.round_number,
            draft_revision_id=response.draft_revision_id,
            model_id=response.model_id,
            mode=response.mode,
            validated_payload=validated_payload,
            canonical_payload_hash=stable_hash(validated_payload),
            response_size_bytes=len(response.raw_bytes),
            usage=response.usage,
            usage_certainty=response.usage_certainty,
        )
        calls[prepared.model_call_key] = current.model_copy(
            update={
                "status": ModelCallStatus.RESPONSE_COMMITTED,
                "response": committed,
                "terminal_at": self._clock.now(),
                "usage_certainty": response.usage_certainty,
            }
        )
        descriptor = self._event(
            operation,
            TraceEventType.VERIFICATION_MODEL_RESPONSE_COMMITTED,
            {
                "stage_key": prepared.stage_key,
                "model_call_key": prepared.model_call_key,
                "response_hash": committed.canonical_payload_hash,
                "usage_certainty": committed.usage_certainty.value,
            },
        )
        self._save_update(
            operation,
            {"model_calls": self._ordered_calls(operation, calls)},
            descriptors=(descriptor,),
        )
        return committed

    def mark_call_terminal(
        self,
        run_id: str,
        prepared: PreparedModelCall,
        *,
        verification_id: str | None = None,
        status: ModelCallStatus,
        failure_code: str,
        usage_certainty: UsageCertainty,
        usage: RuntimeResourceAmount | None = None,
    ) -> None:
        if status not in {
            ModelCallStatus.FAILED,
            ModelCallStatus.TIMED_OUT,
            ModelCallStatus.CANCELLED,
            ModelCallStatus.INTERRUPTED_UNKNOWN,
        }:
            raise VerificationContractError("call terminal status is invalid")
        operation = self._store.load(run_id, verification_id)
        calls, current = self._replace_call(operation, prepared.model_call_key)
        if current.status is not ModelCallStatus.DISPATCHED:
            return
        validate_model_call_transition(current.status, status)
        calls[prepared.model_call_key] = current.model_copy(
            update={
                "status": status,
                "terminal_at": self._clock.now(),
                "failure_code": failure_code,
                "usage_certainty": usage_certainty,
                "terminal_usage": usage,
            }
        )
        if status is ModelCallStatus.CANCELLED:
            operation_status = VerificationOperationStatus.CANCELLED
            event_type = TraceEventType.VERIFICATION_CANCELLED
        elif status is ModelCallStatus.INTERRUPTED_UNKNOWN or (
            status is ModelCallStatus.TIMED_OUT
            and usage_certainty is UsageCertainty.UNKNOWN
        ):
            operation_status = VerificationOperationStatus.INTERRUPTED_UNKNOWN
            event_type = TraceEventType.VERIFICATION_MODEL_CALL_INTERRUPTED_UNKNOWN
        else:
            operation_status = VerificationOperationStatus.FAILED
            event_type = TraceEventType.VERIFICATION_FAILED
        descriptor = self._event(
            operation,
            event_type,
            {
                "stage_key": prepared.stage_key,
                "model_call_key": prepared.model_call_key,
                "failure_code": failure_code,
                "usage_certainty": usage_certainty.value,
            },
        )
        self._save_update(
            operation,
            {
                "model_calls": self._ordered_calls(operation, calls),
                "status": operation_status,
            },
            descriptors=(descriptor,),
        )

    def advance_status(
        self,
        run_id: str,
        target: VerificationOperationStatus,
        *,
        verification_id: str | None = None,
        authority: VerificationResult | None = None,
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if operation.status is target:
            return operation
        validate_operation_transition(operation.status, target)
        update: dict[str, Any] = {"status": target}
        descriptors: tuple[TraceEventDescriptor, ...] = ()
        if target is VerificationOperationStatus.AUTHORITY_COMMITTED:
            if (
                authority is None
                or authority.verification_id != operation.verification_id
            ):
                raise VerificationRecoveryInconsistency(
                    "authority differs from verification operation"
                )
            update["authority_content_hash"] = authority.artifact_content_hash
            if operation.publication_candidate is None:
                raise VerificationRecoveryInconsistency(
                    "authority commit requires an immutable publication candidate"
                )
            descriptors = (
                self._event(
                    operation,
                    TraceEventType.VERIFICATION_AUTHORITY_COMMITTED,
                    {
                        "verification_id": authority.verification_id,
                        "artifact_content_hash": authority.artifact_content_hash,
                    },
                ),
            )
        elif target is VerificationOperationStatus.LIFECYCLE_COMMITTED:
            descriptors = (
                self._event(
                    operation,
                    TraceEventType.VERIFICATION_LIFECYCLE_COMMITTED,
                    {"verification_id": operation.verification_id},
                ),
            )
        return self._save_update(operation, update, descriptors=descriptors)

    def mark_ready_to_publish(
        self,
        run_id: str,
        result: VerificationResult,
        markdown: bytes,
        *,
        expected_prior_verification_id: str | None,
        verification_id: str | None = None,
    ) -> VerificationOperation:
        operation = self._store.load(run_id, verification_id)
        if result.verification_id != operation.verification_id:
            raise VerificationRecoveryInconsistency(
                "publication candidate belongs to another verification operation"
            )
        if (
            expected_prior_verification_id
            != operation.expected_prior_verification_id
        ):
            raise VerificationRecoveryInconsistency(
                "publication predecessor differs from operation pin"
            )
        try:
            markdown_text = markdown.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VerificationRecoveryInconsistency(
                "rendered Markdown is not canonical UTF-8"
            ) from exc
        candidate = VerificationPublicationCandidate(
            publication_key=stable_id(
                "vpub",
                [
                    result.verification_id,
                    expected_prior_verification_id,
                    result.artifact_content_hash,
                    result.markdown_sha256,
                ],
            ),
            result=result,
            markdown_text=markdown_text,
            artifact_content_hash=result.artifact_content_hash,
            markdown_sha256=result.markdown_sha256,
            expected_prior_verification_id=expected_prior_verification_id,
        )
        if operation.status is VerificationOperationStatus.READY_TO_PUBLISH:
            if operation.publication_candidate != candidate:
                raise VerificationRecoveryInconsistency(
                    "publication candidate changed for the same operation"
                )
            return operation
        validate_operation_transition(
            operation.status, VerificationOperationStatus.READY_TO_PUBLISH
        )
        return self._save_update(
            operation,
            {
                "status": VerificationOperationStatus.READY_TO_PUBLISH,
                "publication_candidate": candidate,
            },
        )

    def bootstrap_authority(
        self,
        *,
        frozen: FrozenVerificationInput,
        policy: VerificationPolicy,
        hard_limits: RuntimeResourceAmount,
        authority: VerificationResult,
    ) -> VerificationOperation:
        if (
            authority.run_id != frozen.run_id
            or authority.run_revision != frozen.run_revision
            or authority.claim_snapshot_hash != frozen.claim_snapshot_hash
            or authority.evidence_snapshot_hash != frozen.evidence_snapshot_hash
            or authority.frozen_input_hash != frozen.frozen_input_hash
            or authority.policy_hash != model_sha256(policy)
        ):
            raise VerificationRecoveryInconsistency(
                "legacy authority pins differ from current consistency snapshot"
            )
        operation = self.create(
            frozen=frozen,
            policy=policy,
            hard_limits=hard_limits,
            model_bundle_hash=authority.model_bundle_hash,
            verification_id=authority.verification_id,
            expected_prior_verification_id=(
                authority.supersedes_verification_id
            ),
        )
        self.mark_ready_to_publish(
            operation.run_id,
            authority,
            render_markdown(
                authority.final_draft,
                authority.judge_decisions,
                findings=authority.findings,
                resolved_finding_ids=authority.resolved_finding_ids,
                citation_issues=authority.citation_issues,
                disposition=authority.disposition,
                acquisition_requests=authority.acquisition_requests,
                omitted_claim_ids=authority.omitted_claim_ids,
            ),
            expected_prior_verification_id=authority.supersedes_verification_id,
            verification_id=operation.verification_id,
        )
        return self.advance_status(
            operation.run_id,
            VerificationOperationStatus.AUTHORITY_COMMITTED,
            verification_id=operation.verification_id,
            authority=authority,
        )

    def _save_update(
        self,
        operation: VerificationOperation,
        updates: dict[str, Any],
        *,
        descriptors: tuple[TraceEventDescriptor, ...] = (),
        descriptors_critical: bool = True,
    ) -> VerificationOperation:
        target_status = updates.get("status")
        if (
            isinstance(target_status, VerificationOperationStatus)
            and target_status is not operation.status
        ):
            validate_operation_transition(operation.status, target_status)
        if len(operation.outbox) + len(descriptors) > TOTAL_DESCRIPTOR_SLOTS:
            raise CorruptVerificationOperation("verification outbox is full")
        if (
            sum(item.critical for item in operation.outbox)
            + (len(descriptors) if descriptors_critical else 0)
            > RESERVED_CRITICAL_SLOTS
        ):
            raise CorruptVerificationOperation("verification critical outbox is full")
        raw = operation.model_dump(mode="python")
        raw.update(updates)
        raw.update(
            checkpoint_revision=operation.checkpoint_revision + 1,
            updated_at=self._clock.now(),
            outbox=(
                *operation.outbox,
                *(
                    VerificationOutboxItem(
                        descriptor=item, critical=descriptors_critical
                    )
                    for item in descriptors
                ),
            ),
        )
        updated = VerificationOperation.model_validate(raw)
        self._store.save(updated, expected_revision=operation.checkpoint_revision)
        for descriptor in descriptors:
            try:
                self._deliver(
                    updated.run_id,
                    updated.verification_id,
                    descriptor.event_id,
                )
                updated = self._store.load(
                    updated.run_id, updated.verification_id
                )
            except Exception:
                # The operation/outbox checkpoint is the business boundary.
                # Local trace delivery remains append-once and recoverable.
                updated = self._store.load(
                    updated.run_id, updated.verification_id
                )
        return updated

    def _deliver(self, run_id: str, verification_id: str, event_id: str) -> None:
        if self._recorder is None:
            return
        operation = self._store.load(run_id, verification_id)
        item = next(
            item
            for item in operation.outbox
            if item.descriptor.event_id == event_id
        )
        self._recorder.record_descriptor(
            item.descriptor,
            scope=(
                ObservationScope.RECOVERY
                if "recovery" in item.descriptor.event_type.value
                else ObservationScope.VERIFICATION
            ),
            export=item.descriptor.event_type
            not in {
                TraceEventType.OBSERVABILITY_EXPORT_FAILED,
                TraceEventType.OBSERVABILITY_DELIVERY_DROPPED,
                TraceEventType.OBSERVABILITY_OPTIONAL_OMITTED,
            },
            verification_id=operation.verification_id,
        )
        if item.delivered:
            return
        outbox = tuple(
            current.model_copy(update={"delivered": True})
            if current.descriptor.event_id == event_id
            else current
            for current in operation.outbox
        )
        raw = operation.model_dump(mode="python")
        raw.update(
            checkpoint_revision=operation.checkpoint_revision + 1,
            updated_at=self._clock.now(),
            outbox=outbox,
        )
        updated = VerificationOperation.model_validate(raw)
        self._store.save(updated, expected_revision=operation.checkpoint_revision)

    def _event(
        self,
        operation: VerificationOperation,
        event_type: TraceEventType,
        attributes: dict[str, object],
    ) -> TraceEventDescriptor:
        event = TraceEvent(
            event_id=stable_id(
                "evt",
                [
                    operation.operation_id,
                    event_type.value,
                    attributes,
                ],
            ),
            event_type=event_type,
            timestamp=self._clock.now(),
            run_id=operation.run_id,
            revision=operation.run_revision,
            correlation_id=operation.verification_id,
            causation_id=operation.operation_id,
            attributes={
                "operation_id": operation.operation_id,
                "verification_id": operation.verification_id,
                **attributes,
            },
        )
        return TraceEventDescriptor.from_event(event)

    @staticmethod
    def _replace_call(
        operation: VerificationOperation, model_call_key: str
    ) -> tuple[dict[str, ModelCallRecord], ModelCallRecord]:
        calls = {item.prepared.model_call_key: item for item in operation.model_calls}
        try:
            current = calls[model_call_key]
        except KeyError as exc:
            raise VerificationRecoveryInconsistency(
                "verification call is absent from operation journal"
            ) from exc
        return calls, current

    @staticmethod
    def _ordered_calls(
        operation: VerificationOperation, calls: dict[str, ModelCallRecord]
    ) -> tuple[ModelCallRecord, ...]:
        return tuple(
            calls[item.prepared.model_call_key] for item in operation.model_calls
        )
