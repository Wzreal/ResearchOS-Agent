"""Phase 8 local-first structured observation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from researchos.domain.contracts import ContractModel, SafeId
from researchos.domain.identity import stable_id
from researchos.domain.runtime import TraceEventDescriptor

OBSERVATION_SCHEMA_VERSION = 1


class AppendOnceResult(StrEnum):
    APPENDED = "appended"
    ALREADY_PRESENT = "already_present"


class ExporterOfferResult(StrEnum):
    ACCEPTED = "accepted"
    DROPPED_QUEUE_FULL = "dropped_queue_full"
    DISPATCHER_STOPPED = "dispatcher_stopped"


class ObservationScope(StrEnum):
    RUN = "run"
    TASK = "task"
    ATTEMPT = "attempt"
    TOOL = "tool"
    EVIDENCE = "evidence"
    CLAIM = "claim"
    VERIFICATION = "verification"
    EVALUATION = "evaluation"
    RECOVERY = "recovery"
    OBSERVABILITY = "observability"


class ObservationEnvelope(ContractModel):
    schema_version: Literal[OBSERVATION_SCHEMA_VERSION] = OBSERVATION_SCHEMA_VERSION
    envelope_id: SafeId
    scope: ObservationScope
    descriptor: TraceEventDescriptor
    export: bool = True
    task_id: SafeId | None = None
    attempt_id: SafeId | None = None
    agent_id: SafeId | None = None
    tool_call_id: SafeId | None = None
    evidence_id: SafeId | None = None
    claim_id: SafeId | None = None
    verification_id: SafeId | None = None
    evaluation_id: SafeId | None = None

    @model_validator(mode="after")
    def identity_is_exact(self) -> ObservationEnvelope:
        expected = stable_id(
            "obs", [self.descriptor.event_id, self.descriptor.canonical_event_hash]
        )
        if self.envelope_id != expected:
            raise ValueError("observation envelope identity differs")
        local_only = {
            "observability.export_failed",
            "observability.delivery_dropped",
            "observability.optional_omitted",
        }
        if self.descriptor.event_type.value in local_only and self.export:
            raise ValueError("export diagnostics must be local-only")
        if self.scope in {
            ObservationScope.VERIFICATION,
            ObservationScope.RECOVERY,
        }:
            if self.verification_id is None:
                raise ValueError("verification observation requires verification_id")
            if self.descriptor.correlation_id != self.verification_id:
                raise ValueError("verification observation correlation differs")
        return self


class ExporterPolicy(ContractModel):
    max_pending_deliveries: int = Field(default=256, ge=1, le=10_000)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    export_timeout_ms: int = Field(default=2_000, ge=1, le=60_000)
    max_pending_diagnostics: int = Field(default=64, ge=1, le=1_000)
    max_diagnostic_concurrency: int = Field(default=1, ge=1, le=8)
