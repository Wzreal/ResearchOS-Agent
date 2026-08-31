"""Bounded Phase 8 verification-operation checkpoint contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from researchos.domain.contracts import (
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
    model_sha256,
    validate_sha256,
)
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.runtime import (
    RuntimeResourceAmount,
    TraceEventDescriptor,
    UsageCertainty,
)
from researchos.domain.synthesis import (
    VerificationPolicy,
    VerificationResult,
    VerificationRole,
)

VERIFICATION_RUNTIME_SCHEMA_VERSION = 1
VERIFICATION_OPERATION_VERSION = "verification-operation-v1"
MODEL_REQUEST_SCHEMA_VERSION = "verification-model-request-v1"
DEFAULT_RESPONSE_CONTRACT_VERSION = "verification-response-v1"
MAX_RECOVERY_ATTEMPTS = 16
FIXED_CRITICAL_EVENTS = 12
PER_MODEL_CALL_CRITICAL_EVENTS = 3
PER_RECOVERY_CRITICAL_EVENTS = 2
RESERVED_CRITICAL_SLOTS = 160
TOTAL_DESCRIPTOR_SLOTS = 256
MAX_OPERATION_CHECKPOINT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_OPERATION_CHECKPOINT_BYTES = 16 * 1024 * 1024
MAX_CUMULATIVE_VALIDATED_RESPONSE_BYTES = 32 * 1024 * 1024


class VerificationOperationStatus(StrEnum):
    INITIALIZED = "initialized"
    EXECUTING = "executing"
    READY_TO_PUBLISH = "ready_to_publish"
    AUTHORITY_COMMITTED = "authority_committed"
    LIFECYCLE_COMMITTED = "lifecycle_committed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED_UNKNOWN = "interrupted_unknown"


class ModelCallStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHED = "dispatched"
    RESPONSE_COMMITTED = "response_committed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED_UNKNOWN = "interrupted_unknown"


class PreparedModelCall(ContractModel):
    request_schema_version: Literal[MODEL_REQUEST_SCHEMA_VERSION] = (
        MODEL_REQUEST_SCHEMA_VERSION
    )
    response_contract_version: SafeId
    stage_key: SafeId
    model_call_key: SafeId
    verification_id: SafeId
    model_bundle_hash: Sha256
    role: VerificationRole
    round_number: int = Field(ge=0, le=10)
    draft_revision_id: SafeId
    canonical_request_hash: Sha256
    safe_request_descriptor: dict[str, Any]
    predecessor_response_hashes: tuple[Sha256, ...] = ()

    @model_validator(mode="after")
    def keys_match_pins(self) -> PreparedModelCall:
        expected_stage = stable_id(
            "vstage",
            [
                self.verification_id,
                self.role.value,
                self.round_number,
                self.draft_revision_id,
            ],
        )
        if self.stage_key != expected_stage:
            raise ValueError("model stage key differs")
        expected_call = stable_id(
            "vcall",
            [
                self.stage_key,
                self.model_bundle_hash,
                self.canonical_request_hash,
                self.request_schema_version,
                self.response_contract_version,
                self.predecessor_response_hashes,
            ],
        )
        if self.model_call_key != expected_call:
            raise ValueError("model call key differs")
        allowed = {
            "verification_id",
            "role",
            "round_number",
            "draft_revision_id",
            "frozen_input_hash",
        }
        if set(self.safe_request_descriptor) != allowed:
            raise ValueError("prepared request descriptor contains unapproved fields")
        descriptor = self.safe_request_descriptor
        if (
            descriptor["verification_id"] != self.verification_id
            or descriptor["role"] != self.role.value
            or descriptor["round_number"] != self.round_number
            or descriptor["draft_revision_id"] != self.draft_revision_id
            or not isinstance(descriptor["frozen_input_hash"], str)
            or not validate_sha256(descriptor["frozen_input_hash"])
        ):
            raise ValueError("prepared request descriptor differs from call pins")
        return self


class CommittedModelResponse(ContractModel):
    model_call_key: SafeId
    role: VerificationRole
    round_number: int = Field(ge=0, le=10)
    draft_revision_id: SafeId
    model_id: SafeId
    mode: Literal["mock", "real"]
    validated_payload: dict[str, Any]
    canonical_payload_hash: Sha256
    response_size_bytes: int = Field(ge=0)
    usage: RuntimeResourceAmount
    usage_certainty: UsageCertainty

    @model_validator(mode="after")
    def payload_hash_matches(self) -> CommittedModelResponse:
        if stable_hash(self.validated_payload) != self.canonical_payload_hash:
            raise ValueError("committed response payload hash differs")
        return self


class VerificationPublicationCandidate(ContractModel):
    publication_key: SafeId
    result: VerificationResult
    markdown_text: str
    artifact_content_hash: Sha256
    markdown_sha256: Sha256
    expected_prior_verification_id: SafeId | None = None

    @model_validator(mode="after")
    def candidate_is_exact(self) -> VerificationPublicationCandidate:
        markdown = self.markdown_text.encode("utf-8")
        if self.artifact_content_hash != self.result.artifact_content_hash:
            raise ValueError("publication candidate artifact hash differs")
        if self.markdown_sha256 != self.result.markdown_sha256:
            raise ValueError("publication candidate Markdown pin differs")
        if (
            self.expected_prior_verification_id
            != self.result.supersedes_verification_id
        ):
            raise ValueError("publication candidate supersession pin differs")
        if hashlib.sha256(markdown).hexdigest() != self.markdown_sha256:
            raise ValueError("publication candidate Markdown bytes differ")
        expected_key = stable_id(
            "vpub",
            [
                self.result.verification_id,
                self.expected_prior_verification_id,
                self.artifact_content_hash,
                self.markdown_sha256,
            ],
        )
        if self.publication_key != expected_key:
            raise ValueError("publication candidate key differs")
        return self


class ModelCallRecord(ContractModel):
    prepared: PreparedModelCall
    status: ModelCallStatus
    response: CommittedModelResponse | None = None
    dispatched_at: datetime | None = None
    terminal_at: datetime | None = None
    failure_code: SafeId | None = None
    usage_certainty: UsageCertainty = UsageCertainty.EXACT
    terminal_usage: RuntimeResourceAmount | None = None

    _aware_dispatched = field_validator("dispatched_at")(_require_aware)
    _aware_terminal = field_validator("terminal_at")(_require_aware)

    @model_validator(mode="after")
    def state_is_consistent(self) -> ModelCallRecord:
        if (self.status is ModelCallStatus.RESPONSE_COMMITTED) != (
            self.response is not None
        ):
            raise ValueError("only a committed call has a response")
        if self.response is not None:
            if self.response.model_call_key != self.prepared.model_call_key:
                raise ValueError("response belongs to another call")
            if (
                self.response.role is not self.prepared.role
                or self.response.round_number != self.prepared.round_number
                or self.response.draft_revision_id
                != self.prepared.draft_revision_id
            ):
                raise ValueError("response identity differs from prepared call")
        if self.status is ModelCallStatus.PREPARED and self.dispatched_at is not None:
            raise ValueError("prepared call cannot have dispatch time")
        if self.status is not ModelCallStatus.PREPARED and self.dispatched_at is None:
            raise ValueError("post-prepare call requires dispatch time")
        terminal = self.status not in {
            ModelCallStatus.PREPARED,
            ModelCallStatus.DISPATCHED,
        }
        if terminal != (self.terminal_at is not None):
            raise ValueError("terminal call time differs from call status")
        if (
            self.status is ModelCallStatus.INTERRUPTED_UNKNOWN
            and self.usage_certainty is not UsageCertainty.UNKNOWN
        ):
            raise ValueError("interrupted call usage must be UNKNOWN")
        if (
            terminal
            and self.response is None
            and self.usage_certainty is not UsageCertainty.UNKNOWN
            and self.terminal_usage is None
        ):
            raise ValueError("known terminal call usage must be retained")
        return self


class VerificationOutboxItem(ContractModel):
    descriptor: TraceEventDescriptor
    critical: bool = True
    delivered: bool = False


class VerificationLineage(ContractModel):
    verification_id: SafeId
    citation_ids: tuple[SafeId, ...]
    current_edge_ids: tuple[SafeId, ...]
    evidence_receipt_ids: tuple[SafeId, ...]
    task_ids: tuple[SafeId, ...] = ()
    task_operation_keys: tuple[Sha256, ...] = ()
    attempt_ids: tuple[SafeId, ...] = ()
    tool_call_ids: tuple[SafeId, ...] = ()
    tool_operation_keys: tuple[Sha256, ...] = ()
    adapter_ids: tuple[SafeId, ...] = ()
    diagnostic_assignment_match: bool
    lineage_hash: Sha256

    @model_validator(mode="after")
    def lineage_hash_matches(self) -> VerificationLineage:
        payload = self.model_dump(mode="json", exclude={"lineage_hash"})
        if self.lineage_hash != stable_hash(payload):
            raise ValueError("verification lineage hash differs")
        return self


class VerificationOperation(ContractModel):
    schema_version: Literal[VERIFICATION_RUNTIME_SCHEMA_VERSION] = (
        VERIFICATION_RUNTIME_SCHEMA_VERSION
    )
    operation_version: Literal[VERIFICATION_OPERATION_VERSION] = (
        VERIFICATION_OPERATION_VERSION
    )
    operation_id: SafeId
    verification_id: SafeId
    checkpoint_revision: int = Field(ge=0)
    run_id: SafeId
    run_revision: int = Field(ge=0)
    status: VerificationOperationStatus
    frozen_input_hash: Sha256
    claim_snapshot_hash: Sha256
    evidence_snapshot_hash: Sha256
    verification_policy: VerificationPolicy
    policy_hash: Sha256
    model_bundle_hash: Sha256
    hard_limits: RuntimeResourceAmount
    hard_limits_hash: Sha256
    expected_prior_verification_id: SafeId | None = None
    model_calls: tuple[ModelCallRecord, ...] = ()
    outbox: tuple[VerificationOutboxItem, ...] = ()
    optional_omitted_count: int = Field(default=0, ge=0)
    optional_omitted_hash: Sha256 | None = None
    recovery_attempts: int = Field(default=0, ge=0, le=MAX_RECOVERY_ATTEMPTS)
    publication_candidate: VerificationPublicationCandidate | None = None
    authority_content_hash: Sha256 | None = None
    lineage: VerificationLineage | None = None
    created_at: datetime
    updated_at: datetime
    last_error_code: SafeId | None = None

    _aware_created = field_validator("created_at")(_require_aware)
    _aware_updated = field_validator("updated_at")(_require_aware)

    @model_validator(mode="after")
    def operation_is_consistent(self) -> VerificationOperation:
        if self.operation_id != stable_id(
            "vop", [self.operation_version, self.verification_id]
        ):
            raise ValueError("verification operation identity differs")
        if self.policy_hash != model_sha256(self.verification_policy):
            raise ValueError("verification operation policy hash differs")
        if self.hard_limits_hash != model_sha256(self.hard_limits):
            raise ValueError("verification operation hard-limit hash differs")
        required_critical = (
            FIXED_CRITICAL_EVENTS
            + PER_MODEL_CALL_CRITICAL_EVENTS
            * (1 + 3 * self.verification_policy.max_rounds)
            + PER_RECOVERY_CRITICAL_EVENTS * MAX_RECOVERY_ATTEMPTS
        )
        if required_critical > RESERVED_CRITICAL_SLOTS:
            raise ValueError(
                "verification critical descriptor capacity is insufficient"
            )
        if RESERVED_CRITICAL_SLOTS >= TOTAL_DESCRIPTOR_SLOTS:
            raise ValueError(
                "critical descriptor reservation must be below total capacity"
            )
        expected_verification = stable_id(
            "ver",
            [
                self.run_id,
                self.run_revision,
                self.claim_snapshot_hash,
                self.evidence_snapshot_hash,
                self.policy_hash,
                self.model_bundle_hash,
            ],
        )
        if self.verification_id != expected_verification:
            raise ValueError("verification operation pins differ")
        if self.updated_at < self.created_at:
            raise ValueError("operation update precedes creation")
        call_keys = [item.prepared.model_call_key for item in self.model_calls]
        stage_keys = [item.prepared.stage_key for item in self.model_calls]
        if len(call_keys) != len(set(call_keys)) or len(stage_keys) != len(
            set(stage_keys)
        ):
            raise ValueError("verification operation contains duplicate call identity")
        event_ids = [item.descriptor.event_id for item in self.outbox]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("verification outbox contains duplicate event identity")
        if len(self.outbox) > TOTAL_DESCRIPTOR_SLOTS:
            raise ValueError("verification outbox exceeds total capacity")
        if sum(item.critical for item in self.outbox) > RESERVED_CRITICAL_SLOTS:
            raise ValueError("verification outbox exceeds critical capacity")
        if self.optional_omitted_count and self.optional_omitted_hash is None:
            raise ValueError("omitted optional observations require a summary hash")
        if self.status in {
            VerificationOperationStatus.READY_TO_PUBLISH,
            VerificationOperationStatus.AUTHORITY_COMMITTED,
            VerificationOperationStatus.LIFECYCLE_COMMITTED,
            VerificationOperationStatus.COMPLETED,
        } and self.publication_candidate is None:
            raise ValueError("publication-ready status requires immutable candidate")
        if self.status in {
            VerificationOperationStatus.INITIALIZED,
            VerificationOperationStatus.EXECUTING,
        } and self.publication_candidate is not None:
            raise ValueError("pre-publication status cannot contain a candidate")
        if self.status in {
            VerificationOperationStatus.AUTHORITY_COMMITTED,
            VerificationOperationStatus.LIFECYCLE_COMMITTED,
            VerificationOperationStatus.COMPLETED,
        } and self.authority_content_hash is None:
            raise ValueError("committed authority status requires authority hash")
        if (
            self.authority_content_hash is not None
            and self.publication_candidate is not None
            and self.publication_candidate.artifact_content_hash
            != self.authority_content_hash
        ):
            raise ValueError("publication candidate and authority hashes differ")
        if (
            self.publication_candidate is not None
            and self.publication_candidate.result.verification_id
            != self.verification_id
        ):
            raise ValueError("publication candidate belongs to another verification")
        if (
            self.publication_candidate is not None
            and self.publication_candidate.expected_prior_verification_id
            != self.expected_prior_verification_id
        ):
            raise ValueError("publication candidate predecessor pin differs")
        cumulative_payload_bytes = sum(
            len(
                json.dumps(
                    item.response.validated_payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
            for item in self.model_calls
            if item.response is not None
        )
        if cumulative_payload_bytes > MAX_CUMULATIVE_VALIDATED_RESPONSE_BYTES:
            raise ValueError("committed response payloads exceed durable hard cap")
        return self


class VerificationOperationEnvelope(ContractModel):
    schema_version: Literal[VERIFICATION_RUNTIME_SCHEMA_VERSION] = (
        VERIFICATION_RUNTIME_SCHEMA_VERSION
    )
    operation: VerificationOperation
    payload_sha256: Sha256

    @model_validator(mode="after")
    def payload_hash_matches(self) -> VerificationOperationEnvelope:
        if self.payload_sha256 != model_sha256(self.operation):
            raise ValueError("verification operation envelope hash differs")
        return self


def max_model_calls(policy: VerificationPolicy) -> int:
    return 1 + 3 * policy.max_rounds


def max_critical_descriptors(policy: VerificationPolicy) -> int:
    return (
        FIXED_CRITICAL_EVENTS
        + PER_MODEL_CALL_CRITICAL_EVENTS * max_model_calls(policy)
        + PER_RECOVERY_CRITICAL_EVENTS * MAX_RECOVERY_ATTEMPTS
    )


def validate_operation_capacity(policy: VerificationPolicy) -> None:
    if max_critical_descriptors(policy) > RESERVED_CRITICAL_SLOTS:
        raise ValueError("verification critical descriptor capacity is insufficient")
    if RESERVED_CRITICAL_SLOTS >= TOTAL_DESCRIPTOR_SLOTS:
        raise ValueError("critical descriptor reservation must be below total capacity")
