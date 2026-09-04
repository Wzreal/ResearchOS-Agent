"""Ephemeral Phase 9B REAL Tool dispatch contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from researchos.domain.agent import AgentToolCall, AgentToolDecision
from researchos.domain.contracts import ContractModel, SafeId, Sha256, model_sha256
from researchos.domain.identity import stable_hash
from researchos.domain.planning import ResearchTask
from researchos.domain.tools import BrowserRequest, SearchRequest, ToolInvocationRequest


class RealWebAgentToolCall(AgentToolCall):
    capability_id: Literal["web_browser", "web_search"]
    input: Annotated[SearchRequest | BrowserRequest, Field(discriminator="input_type")]

    @model_validator(mode="after")
    def capability_matches_input(self) -> RealWebAgentToolCall:
        expected = (
            "web_search" if isinstance(self.input, SearchRequest) else "web_browser"
        )
        if self.capability_id != expected:
            raise ValueError("REAL web capability differs from Tool input")
        return self


class RealWebAgentToolDecision(AgentToolDecision):
    tool_call: RealWebAgentToolCall


class CapabilityDispatchContext(ContractModel):
    schema_version: Literal[1] = 1
    run_id: SafeId
    task: ResearchTask
    task_id: SafeId
    task_contract_hash: Sha256
    task_operation_key: Sha256
    requested_capability_id: SafeId
    authorized_capability_ids: tuple[SafeId, ...]
    descriptor_hash: Sha256
    tool_operation_key: Sha256
    context_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> CapabilityDispatchContext:
        if self.task.task_id != self.task_id:
            raise ValueError("dispatch task identity differs")
        if self.task_contract_hash != model_sha256(self.task):
            raise ValueError("dispatch task hash differs")
        if self.authorized_capability_ids != self.task.required_capability_ids:
            raise ValueError("dispatch capabilities differ from task authority")
        if self.requested_capability_id not in self.authorized_capability_ids:
            raise ValueError("dispatch capability is not task-authorized")
        if self.context_hash != self.compute_hash():
            raise ValueError("dispatch context hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"context_hash"}))

    @classmethod
    def build(cls, **values: object) -> CapabilityDispatchContext:
        candidate = cls.model_construct(**values, context_hash="0" * 64)
        payload = candidate.model_dump(mode="json", exclude={"context_hash"})
        return cls.model_validate({**payload, "context_hash": stable_hash(payload)})


class AuthorizedToolDispatchEnvelope(ContractModel):
    schema_version: Literal[1] = 1
    invocation: ToolInvocationRequest
    dispatch_context: CapabilityDispatchContext

    @model_validator(mode="after")
    def invocation_matches_context(self) -> AuthorizedToolDispatchEnvelope:
        context = self.dispatch_context
        invocation = self.invocation
        if (
            invocation.run_id != context.run_id
            or invocation.task_id != context.task_id
            or invocation.tool_operation_key != context.tool_operation_key
        ):
            raise ValueError("authorized Tool envelope identity differs")
        return self
