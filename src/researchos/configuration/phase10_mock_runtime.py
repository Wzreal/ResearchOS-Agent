"""Ephemeral deterministic adapters for the Phase 10 MOCK configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from researchos.adapters.mock_agent import (
    AgentFixture,
    AgentFixtureKey,
    ScriptedAgent,
)
from researchos.adapters.mock_claim_extraction import (
    ClaimExtractionFixtureKey,
    MockClaimExtractionModel,
)
from researchos.adapters.mock_planning import MockPlanningModel, PlanningFixtureKey
from researchos.adapters.mock_verification import (
    MockVerificationModel,
    VerificationFixture,
    VerificationFixtureKey,
)
from researchos.adapters.sleeper import ControlledSleeper
from researchos.application.agent_runner import AgentRunnerPolicy
from researchos.configuration.phase10_mock import Phase10MockWorkflowBundleV1
from researchos.domain.agent import (
    AgentDescriptor,
    AgentFinalDecision,
    AgentFinalResult,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.claim_extraction import (
    ClaimCandidate,
    ClaimEvidencePin,
    ClaimExtractionResponse,
)
from researchos.domain.claims import ClaimEvidenceRelation
from researchos.domain.planning import PlanningModelResponse, PlanningRequest
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.synthesis import JudgeVerdict, VerificationRole
from researchos.domain.tools import (
    AdapterMode,
    SearchHit,
    SearchRequest,
    SearchResult,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)
from researchos.interfaces.runtime import CancellationSignal

MOCK_OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class MockRuntimeSleeper:
    """The executor cancels this timeout sleeper when work completes."""

    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


class DeterministicMockSearchTool:
    descriptor = ToolDescriptor(
        tool_id="phase10_search",
        capability_id="search",
        adapter_id="phase10_mock_search",
        mode=AdapterMode.MOCK,
        operation_version="v1",
        input_type="search",
        output_type="search_result",
        side_effect=ToolSideEffect.NONE,
        idempotency=IdempotencyMode.IDEMPOTENT,
    )

    def __init__(self) -> None:
        self.invocation_count = 0

    async def invoke(self, request, cancellation) -> ToolInvocationResult:
        del request, cancellation
        self.invocation_count += 1
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(
                adapter_id=self.descriptor.adapter_id,
                hits=(
                    SearchHit(
                        locator="phase10-mock-source",
                        url="https://example.com/phase10-mock-source",
                        title="Phase 10 deterministic source",
                        snippet="The deterministic Phase 10 claim is supported.",
                        retrieved_at=MOCK_OBSERVED_AT,
                        adapter_id=self.descriptor.adapter_id,
                    ),
                ),
            ),
            usage=ToolUsage(tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


class Phase10MockClaimExtractionModel:
    model_bundle_hash = MockClaimExtractionModel.model_bundle_hash

    def __init__(self) -> None:
        self.invocation_count = 0

    def generate(self, request):
        self.invocation_count += 1
        evidence = request.evidence_items[0]
        response = ClaimExtractionResponse(
            run_id=request.run_id,
            extraction_id=request.extraction_id,
            candidates=(
                ClaimCandidate(
                    ordinal=1,
                    statement="The deterministic Phase 10 claim is supported.",
                    evidence_pins=(
                        ClaimEvidencePin(
                            evidence_id=evidence.evidence_id,
                            evidence_revision=evidence.revision,
                            relation=ClaimEvidenceRelation.SUPPORTS,
                        ),
                    ),
                ),
            ),
        )
        model = MockClaimExtractionModel(
            {ClaimExtractionFixtureKey(request.evidence_snapshot_hash): response}
        )
        return model.generate(request)


class Phase10MockVerificationModel:
    def __init__(self) -> None:
        self._identity = MockVerificationModel({})
        self.requests = []

    @property
    def model_bundle_hash(self) -> str:
        return self._identity.model_bundle_hash

    async def invoke(self, request, cancellation: CancellationSignal):
        self.requests.append(request)
        if request.role is VerificationRole.SYNTHESIZER:
            claims = tuple(
                {
                    "claim_id": item["record"]["claim_id"],
                    "claim_revision": item["record"]["current_revision"],
                    "section_ordinal": 1,
                    "prose": item["revision"]["statement"],
                    "evidence_ids": tuple(
                        citation["evidence_id"]
                        for citation in request.context["citation_allowlist"]
                        if citation["claim_id"] == item["record"]["claim_id"]
                    ),
                }
                for item in request.context["claims"]
            )
            payload = {"section_titles": ["Findings"], "claims": claims}
        elif request.role is VerificationRole.RED:
            payload = {"findings": []}
        elif request.role is VerificationRole.BLUE:
            payload = {"actions": []}
        else:
            payload = {
                "draft_revision_id": request.draft_revision_id,
                "action": "finalize",
                "finding_dispositions": [],
                "decisions": [
                    {
                        "report_claim_id": item["report_claim_id"],
                        "verdict": JudgeVerdict.SUPPORTED.value,
                    }
                    for item in request.context["draft"]["report_claims"]
                ],
            }
        key = VerificationFixtureKey(
            request.verification_id,
            request.role,
            request.round_number,
            request.draft_revision_id,
        )
        model = MockVerificationModel({key: VerificationFixture(payload=payload)})
        return await model.invoke(request, cancellation)


@dataclass(frozen=True, slots=True)
class Phase10MockRuntime:
    planning_model: MockPlanningModel
    agent: ScriptedAgent
    tools: tuple[DeterministicMockSearchTool, ...]
    claim_extraction_model: Phase10MockClaimExtractionModel
    verification_model: Phase10MockVerificationModel
    agent_policy: AgentRunnerPolicy
    runner_sleeper: ControlledSleeper
    executor_sleeper: MockRuntimeSleeper
    semantic_identity: tuple[str, ...]


def build_phase10_mock_runtime(
    bundle: Phase10MockWorkflowBundleV1, query: str
) -> Phase10MockRuntime:
    normalized = query.strip()
    if not normalized:
        raise ValueError("Phase 10 MOCK query must not be blank")
    planning = MockPlanningModel(
        {PlanningFixtureKey(normalized, 0, None): _planning_response}
    )
    outputs = {
        "task_a": ("sources", "application/json"),
        "task_b": ("risks", "application/json"),
        "task_c": ("comparison", "text/markdown"),
    }
    fixtures = {
        AgentFixtureKey("task_a", 1, 1): AgentFixture(
            decision=AgentToolDecision(
                tool_call=AgentToolCall(
                    tool_call_id="phase10_search_call",
                    capability_id="search",
                    input=SearchRequest(query=normalized),
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
    }
    for task_id, step in (("task_a", 2), ("task_b", 1), ("task_c", 1)):
        output_id, media_type = outputs[task_id]
        fixtures[AgentFixtureKey(task_id, 1, step)] = AgentFixture(
            decision=AgentFinalDecision(
                final=AgentFinalResult(
                    outputs=(
                        AgentProducedOutput(output_id=output_id, media_type=media_type),
                    )
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
    return Phase10MockRuntime(
        planning_model=planning,
        agent=ScriptedAgent(
            AgentDescriptor(
                agent_id="phase10_mock_agent",
                adapter_id=bundle.runtime_adapter_id,
                mode=AdapterMode.MOCK,
                operation_version="v1",
            ),
            fixtures,
        ),
        tools=(DeterministicMockSearchTool(),),
        claim_extraction_model=Phase10MockClaimExtractionModel(),
        verification_model=Phase10MockVerificationModel(),
        agent_policy=AgentRunnerPolicy(max_agent_steps=2, max_tool_calls=1),
        runner_sleeper=ControlledSleeper(),
        executor_sleeper=MockRuntimeSleeper(),
        semantic_identity=(
            bundle.bundle_hash,
            normalized,
            bundle.planning_adapter_id,
            bundle.runtime_adapter_id,
            bundle.claim_extraction_adapter_id,
            bundle.verification_adapter_id,
        ),
    )


def _planning_response(request: PlanningRequest) -> PlanningModelResponse:
    payload = {
        "schema_version": 1,
        "run_id": request.run_id,
        "plan_id": request.plan_id,
        "perspectives": [
            {
                "perspective_id": "perspective_primary",
                "name": "primary",
                "title": "Primary",
                "goal": "Establish deterministic facts",
                "rationale": "Ground the MOCK workflow",
                "priority": 80,
            },
            {
                "perspective_id": "perspective_risk",
                "name": "risk",
                "title": "Risk",
                "goal": "Check deterministic counterevidence",
                "rationale": "Exercise parallel planning",
                "priority": 60,
            },
        ],
        "tasks": [
            _task(
                "task_a",
                "perspective_primary",
                "sources",
                "application/json",
                ("search",),
            ),
            _task(
                "task_b",
                "perspective_risk",
                "risks",
                "application/json",
                ("read",),
            ),
            _task(
                "task_c",
                "perspective_primary",
                "comparison",
                "text/markdown",
                (),
                ("task_a",),
            ),
        ],
        "planner_metadata": {
            "metadata_version": 1,
            "planning_model_id": "mock_model",
            "planner_id": "perspective_planner",
            "planner_version": "phase10-mock-v1",
        },
    }
    return PlanningModelResponse(planning_model_id="mock_model", payload=payload)


def _task(
    task_id, perspective_id, output_id, media_type, capabilities, dependencies=()
):
    return {
        "task_id": task_id,
        "perspective_id": perspective_id,
        "objective": f"Execute deterministic {task_id}",
        "dependencies": [{"task_id": item} for item in dependencies],
        "required_capability_ids": list(capabilities),
        "expected_outputs": [
            {"output_id": output_id, "description": output_id, "media_type": media_type}
        ],
        "estimate": {
            "duration_milliseconds": 1_000,
            "tokens": 100,
            "cost_microunits": 100,
            "tool_calls": 1 if capabilities else 0,
        },
        "policy": {"priority": 50, "required": True},
    }
