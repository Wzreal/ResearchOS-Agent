"""Provider-independent bounded Agent contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256, _require_aware
from researchos.domain.planning import ExpectedOutput, ResearchTask
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import AdapterMode, ToolInput, ToolInvocationResult

AGENT_SCHEMA_VERSION = 1


class AgentDecisionKind(StrEnum):
    TOOL_CALL = "tool_call"
    FINAL = "final"
    FAILED = "failed"


class AgentExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AgentDescriptor(ContractModel):
    schema_version: Literal[AGENT_SCHEMA_VERSION] = AGENT_SCHEMA_VERSION
    agent_id: SafeId
    adapter_id: SafeId
    mode: AdapterMode
    operation_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]


class AgentError(ContractModel):
    code: SafeId
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    retryable: bool = False


class AgentProducedOutput(ContractModel):
    output_id: SafeId
    media_type: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    artifact_ids: tuple[SafeId, ...] = ()
    value_hash: Sha256 | None = None


class AgentFinalResult(ContractModel):
    outputs: tuple[AgentProducedOutput, ...]

    @model_validator(mode="after")
    def output_ids_are_unique(self) -> AgentFinalResult:
        ids = [item.output_id for item in self.outputs]
        if len(ids) != len(set(ids)):
            raise ValueError("agent final output IDs must be unique")
        return self


class AgentToolCall(ContractModel):
    tool_call_id: SafeId
    capability_id: SafeId
    input: ToolInput


class AgentObservation(ContractModel):
    agent_step: int = Field(ge=1)
    tool_call_id: SafeId
    capability_id: SafeId
    tool_id: SafeId
    adapter_id: SafeId
    tool_operation_key: Sha256
    result: ToolInvocationResult


class AgentContext(ContractModel):
    run_id: SafeId
    run_revision: int = Field(ge=0)
    dag_id: SafeId
    task: ResearchTask
    task_id: SafeId
    attempt_id: SafeId
    attempt_number: int = Field(ge=1)
    task_operation_key: Sha256
    task_attempt_key: Sha256
    task_idempotency: IdempotencyMode
    deadline: datetime
    hard_limits: RuntimeResourceAmount
    prior_backend_receipt: SafeId | None = None
    expected_outputs: tuple[ExpectedOutput, ...]
    authorized_capability_ids: tuple[SafeId, ...]

    _aware = field_validator("deadline")(_require_aware)

    @model_validator(mode="after")
    def task_authorization_is_exact(self) -> AgentContext:
        if self.task.task_id != self.task_id:
            raise ValueError("Agent context task identity differs")
        if self.authorized_capability_ids != self.task.required_capability_ids:
            raise ValueError("Agent authorization must equal task capabilities")
        return self


class AgentRequest(ContractModel):
    request_id: SafeId
    context: AgentContext
    agent_step: int = Field(ge=1)
    observations: tuple[AgentObservation, ...] = ()


class _AgentDecisionUsage(ContractModel):
    usage: RuntimeResourceAmount | None
    usage_certainty: UsageCertainty

    @model_validator(mode="after")
    def usage_is_consistent(self) -> _AgentDecisionUsage:
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown Agent usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known Agent usage requires an amount")
        if self.usage is not None and self.usage.tool_calls:
            raise ValueError("Agent decision usage cannot report Tool calls")
        return self


class AgentToolDecision(_AgentDecisionUsage):
    kind: Literal[AgentDecisionKind.TOOL_CALL] = AgentDecisionKind.TOOL_CALL
    tool_call: AgentToolCall


class AgentFinalDecision(_AgentDecisionUsage):
    kind: Literal[AgentDecisionKind.FINAL] = AgentDecisionKind.FINAL
    final: AgentFinalResult


class AgentFailedDecision(_AgentDecisionUsage):
    kind: Literal[AgentDecisionKind.FAILED] = AgentDecisionKind.FAILED
    error: AgentError


AgentDecision = Annotated[
    AgentToolDecision | AgentFinalDecision | AgentFailedDecision,
    Field(discriminator="kind"),
]


class AgentExecutionResult(ContractModel):
    status: AgentExecutionStatus
    final: AgentFinalResult | None = None
    error: AgentError | None = None
    usage: RuntimeResourceAmount | None = None
    usage_certainty: UsageCertainty = UsageCertainty.UNKNOWN
    backend_receipt: SafeId | None = None

    @model_validator(mode="after")
    def result_is_consistent(self) -> AgentExecutionResult:
        succeeded = self.status is AgentExecutionStatus.SUCCEEDED
        if succeeded != (self.final is not None) or succeeded == (
            self.error is not None
        ):
            raise ValueError("agent success requires final; failure requires error")
        if self.usage_certainty is UsageCertainty.UNKNOWN and self.usage is not None:
            raise ValueError("unknown agent usage cannot contain an amount")
        if self.usage_certainty is not UsageCertainty.UNKNOWN and self.usage is None:
            raise ValueError("known agent usage requires an amount")
        return self
