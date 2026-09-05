"""Offline Phase 11 portfolio E2E through the production REAL composition path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.workflow_factory import WorkflowFactory
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.configuration.phase10_mock_runtime import (
    Phase10MockClaimExtractionModel,
    Phase10MockVerificationModel,
)
from researchos.configuration.phase11 import (
    Phase11RealBenchmarkBundleV1,
    phase11_evaluation_policy,
    phase11_real_run_config,
)
from researchos.configuration.phase11_benchmark import build_phase11_real_benchmark_v1
from researchos.domain.agent import (
    AgentFinalDecision,
    AgentFinalResult,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.contracts import (
    RunInput,
    RunStatus,
    TraceEventType,
    model_sha256,
)
from researchos.domain.planning import PlanningModelResponse
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.tools import (
    AdapterMode,
    SearchContentKind,
    SearchHitV2,
    SearchRequest,
    SearchResultV2,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolUsage,
)
from researchos.domain.workflow import WorkflowBudgetAllocation
from researchos.interfaces.providers import ProviderAdmissionProfile

_NOW = datetime(2026, 9, 6, tzinfo=UTC)
_DYNAMIC_TASK_IDS = ("artemis_mission", "artemis_crew")


def _settings():
    values = {
        "RESEARCHOS_MAX_INPUT_TOKENS": "8192",
        "RESEARCHOS_MAX_OUTPUT_TOKENS": "4096",
        "RESEARCHOS_MAX_REQUEST_BYTES": "1048576",
        "RESEARCHOS_MAX_RESPONSE_BYTES": "1048576",
        "RESEARCHOS_CONNECT_TIMEOUT_MS": "10000",
        "RESEARCHOS_READ_TIMEOUT_MS": "60000",
        "RESEARCHOS_WRITE_TIMEOUT_MS": "10000",
        "RESEARCHOS_POOL_TIMEOUT_MS": "10000",
        "RESEARCHOS_COST_CURRENCY": "USD",
        "RESEARCHOS_MAX_COST_MICROUNITS_PER_CALL": "11000000",
        "RESEARCHOS_PRICING_POLICY_VERSION": "upper-bound-v1",
        "RESEARCHOS_INPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "10000000",
        "RESEARCHOS_OUTPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "20000000",
        "RESEARCHOS_DEEPSEEK_BASE_ENDPOINT": "https://api.deepseek.com/",
        "RESEARCHOS_DEEPSEEK_ADAPTER_VERSION": "v1",
        "RESEARCHOS_REAL_CAPABILITIES": "web_search",
        "RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS": "90000",
        "RESEARCHOS_PHASE11_PLANNING_TOTAL_CALL_TIMEOUT_MS": "90000",
        "RESEARCHOS_TAVILY_MICROUNITS_PER_CREDIT": "100000",
    }
    for role, model in {
        "PLANNING": "deepseek-v4-pro",
        "AGENT": "deepseek-v4-flash",
        "CLAIM_EXTRACTION": "deepseek-v4-pro",
        "VERIFICATION": "deepseek-v4-pro",
    }.items():
        values[f"RESEARCHOS_DEEPSEEK_{role}_MODEL"] = model
        values[f"RESEARCHOS_DEEPSEEK_{role}_THINKING_MODE"] = "enabled"
        values[f"RESEARCHOS_DEEPSEEK_{role}_REASONING_EFFORT"] = "high"
    return load_real_integration_settings(values, require_phase10_roles=True)


def _portfolio_profile():
    profile = build_phase10_mock_bundle_v1().workflow_profile
    allocation = WorkflowBudgetAllocation(
        total=RuntimeResourceAmount(
            duration_milliseconds=1_080_000,
            tokens=147_456,
            cost_microunits=2_266_080,
            tool_calls=15,
        ),
        planning=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=12_288,
            cost_microunits=163_840,
            tool_calls=1,
        ),
        execution=RuntimeResourceAmount(
            duration_milliseconds=540_000,
            tokens=73_728,
            cost_microunits=1_283_040,
            tool_calls=9,
        ),
        claim_extraction=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=12_288,
            cost_microunits=163_840,
            tool_calls=1,
        ),
        verification=RuntimeResourceAmount(
            duration_milliseconds=360_000,
            tokens=49_152,
            cost_microunits=655_360,
            tool_calls=4,
        ),
    )
    template = profile.execution.task_policies[0].model_copy(
        update={
            "timeout_milliseconds": 180_000,
            "reservation": RuntimeResourceAmount(
                duration_milliseconds=180_000,
                tokens=24_576,
                cost_microunits=427_680,
                tool_calls=3,
            ),
        }
    )
    return profile.model_copy(
        update={
            "profile_id": "phase11_portfolio_smoke",
            "profile_version": "1",
            "system_commit_sha": "a" * 40,
            "system_version": "phase11-portfolio-smoke-v1",
            "budget": profile.budget.model_copy(
                update={
                    "profile_id": "phase11_portfolio_smoke_budget",
                    "profile_version": "1",
                    "allocation": allocation,
                }
            ),
            "execution": profile.execution.model_copy(
                update={
                    "task_policies": tuple(
                        template.model_copy(update={"task_id": task_id})
                        for task_id in ("task_a", "task_b", "task_c")
                    )
                }
            ),
        }
    )


def _bundle(settings):
    profile = _portfolio_profile()
    return Phase11RealBenchmarkBundleV1(
        bundle_id="phase11_portfolio_smoke",
        bundle_version="1",
        workflow_profile=profile,
        benchmark=build_phase11_real_benchmark_v1(),
        evaluation_policy=phase11_evaluation_policy(profile),
        real_settings_hash=model_sha256(settings),
        capability_ids=("web_search",),
        approved_refs=("feat/phase-11-real-evaluation",),
        max_agent_steps=2,
        max_agent_tool_calls=1,
        system_commit_sha=profile.system_commit_sha,
        system_version=profile.system_version,
        max_batch_cost_microunits=50_000_000,
    )


class _FakePlanning:
    def __init__(self) -> None:
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return PlanningModelResponse(
            planning_model_id="deepseek-v4-pro",
            payload={
                "schema_version": 1,
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "perspectives": [
                    {
                        "perspective_id": "artemis_research",
                        "name": "artemis",
                        "title": "Artemis",
                        "goal": "Find the mission and crew pages",
                        "rationale": "Exercise dynamic Phase 11 tasks",
                        "priority": 50,
                    }
                ],
                "tasks": [
                    {
                        "task_id": task_id,
                        "perspective_id": "artemis_research",
                        "objective": f"Research {task_id}",
                        "dependencies": (
                            []
                            if index == 0
                            else [{"task_id": _DYNAMIC_TASK_IDS[index - 1]}]
                        ),
                        "required_capability_ids": ["web_search"],
                        "expected_outputs": [
                            {
                                "output_id": f"{task_id}_output",
                                "description": f"{task_id} output",
                                "media_type": "application/json",
                            }
                        ],
                        "estimate": {
                            "duration_milliseconds": 1_000,
                            "tokens": 100,
                            "cost_microunits": 100,
                            "tool_calls": 1,
                        },
                        "policy": {"priority": 50, "required": True},
                    }
                    for index, task_id in enumerate(_DYNAMIC_TASK_IDS)
                ],
                "planner_metadata": {
                    "metadata_version": 1,
                    "planning_model_id": "deepseek-v4-pro",
                    "planner_id": "perspective_planner",
                    "planner_version": "phase11-offline-e2e-v1",
                },
            },
        )


class _FakeAgent:
    def __init__(self, reservation) -> None:
        self.provider_call_reservation = reservation
        self.provider_admission_profile = (
            ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1
        )
        self.requests = []

    async def decide(self, request, _cancellation):
        self.requests.append(request)
        if request.agent_step == 1:
            return AgentToolDecision(
                tool_call=AgentToolCall(
                    tool_call_id=f"{request.context.task_id}_search",
                    capability_id="web_search",
                    input=SearchRequest(query=request.context.task.objective),
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        output = request.context.expected_outputs[0]
        return AgentFinalDecision(
            final=AgentFinalResult(
                outputs=(
                    AgentProducedOutput(
                        output_id=output.output_id, media_type=output.media_type
                    ),
                )
            ),
            usage=RuntimeResourceAmount(),
            usage_certainty=UsageCertainty.EXACT,
        )


class _FakeWebSearch:
    def __init__(self, descriptor, reservation) -> None:
        self.descriptor = descriptor
        self.provider_call_reservation = reservation
        self.invocations = []

    async def invoke_authorized(self, envelope, _cancellation):
        self.invocations.append(envelope)
        locator = (
            "https://www.nasa.gov/mission/artemis-ii/"
            if envelope.invocation.task_id == "artemis_mission"
            else "https://www.nasa.gov/humans-in-space/artemis-ii/"
        )
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResultV2(
                adapter_id=self.descriptor.adapter_id,
                hits=(
                    SearchHitV2(
                        locator=locator,
                        url=locator,
                        title="Offline NASA Artemis reference",
                        snippet="Deterministic offline Artemis evidence.",
                        content_kind=SearchContentKind.SOURCE_EXCERPT,
                        retrieved_at=_NOW,
                        adapter_id=self.descriptor.adapter_id,
                        provenance={"provider_id": "offline_fake"},
                    ),
                ),
            ),
            usage=ToolUsage(
                duration_milliseconds=1,
                cost_microunits=self.provider_call_reservation.cost_microunits,
                tool_calls=1,
            ),
            usage_certainty=UsageCertainty.UPPER_BOUND,
        )


class _FakeVerification:
    def __init__(self) -> None:
        self._delegate = Phase10MockVerificationModel()
        self.requests = []

    async def invoke(self, request, cancellation):
        self.requests.append(request)
        response = await self._delegate.invoke(request, cancellation)
        return response.model_copy(update={"mode": "real"})


class _FakeIntegrations:
    def __init__(self, settings) -> None:
        self.planning = _FakePlanning()
        self.agent_adapter = _FakeAgent(
            settings.model_for_role("agent").suboperation_reservation()
        )
        capability = settings.capability_for_id("web_search")
        self.search = _FakeWebSearch(
            capability.descriptor(), capability.provider_reservation
        )
        self.claim_extraction = Phase10MockClaimExtractionModel()
        self.verification = _FakeVerification()
        self._registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.REAL}))
        self._registry.register(self.search)

    def planning_model(self, _run_id):
        return self.planning

    def agent(self, _run_id):
        return self.agent_adapter

    def capability_registry(self, _run_id):
        return self._registry

    def claim_extraction_model(self, _run_id, *, workflow_budget_slice):
        del workflow_budget_slice
        return self.claim_extraction

    def verification_model(self, _run_id):
        return self.verification


def test_phase11_portfolio_real_composition_completes_offline_dynamic_two_task_case(
    tmp_path,
) -> None:
    settings = _settings()
    bundle = _bundle(settings)
    factory = WorkflowFactory(tmp_path)
    coordinator = factory.build_real(
        bundle=bundle,
        settings=settings,
        secrets=EnvironmentSecretSource(
            {
                "researchos_deepseek_api_key": "offline-test-secret",
                "researchos_tavily_api_key": "offline-test-secret",
            }
        ),
    )
    runtime = coordinator._runtime_binder.__self__
    fake = _FakeIntegrations(settings)
    runtime._integrations = fake
    case = next(
        item
        for item in bundle.benchmark.cases
        if item.case_id == "p11_citation_chain"
    )

    state = asyncio.run(
        coordinator.create_and_execute(
            RunInput(query=case.query), phase11_real_run_config(bundle, settings)
        )
    )
    evaluation = asyncio.run(
        factory.evaluate_phase11(
            bundle=bundle,
            run_id=state.run_id,
            case_id=case.case_id,
            cancellation=coordinator._cancellation,
        )
    )

    assert state.status is RunStatus.COMPLETED
    assert len(fake.planning.requests) == 1
    assert len(fake.agent_adapter.requests) == 4
    assert len(fake.search.invocations) == 2
    assert fake.claim_extraction.invocation_count == 1
    assert len(fake.verification.requests) == 4
    trace = FilesystemTraceSink(tmp_path).read(state.run_id)
    assert TraceEventType.PLANNING_VALIDATED in {item.event_type for item in trace}
    assert sum(item.event_type is TraceEventType.AGENT_COMPLETED for item in trace) == 2
    tool_completed = sum(
        item.event_type is TraceEventType.TOOL_INVOCATION_SUCCEEDED for item in trace
    )
    assert tool_completed == 2
    assert FilesystemEvidenceStore(tmp_path).load(state.run_id).evidence
    graph = FilesystemClaimGraphStore(tmp_path).load(state.run_id)
    assert graph.claims and graph.claim_revisions
    assert graph.edges and graph.edge_revisions and graph.receipts
    assert evaluation.eval_run_id
    assert list((tmp_path / "evaluations").rglob("*.json"))
    checkpoint = FilesystemCheckpointStore(tmp_path).load(state.run_id)
    assert {task.task_id for task in checkpoint.dag.tasks} == set(_DYNAMIC_TASK_IDS)
    assert {
        policy.task_id for policy in checkpoint.execution_policy.tasks
    } == set(_DYNAMIC_TASK_IDS)
    resumed = WorkflowFactory(tmp_path).build_real(
        bundle=bundle,
        settings=settings,
        secrets=EnvironmentSecretSource(
            {
                "researchos_deepseek_api_key": "offline-test-secret",
                "researchos_tavily_api_key": "offline-test-secret",
            }
        ),
    )
    assert asyncio.run(
        resumed.resume_and_execute(
            state.run_id,
            expected_input=RunInput(query=case.query),
            expected_config=phase11_real_run_config(bundle, settings),
        )
    ) == state
    persisted = FilesystemRunStore(tmp_path).load(state.run_id)
    assert persisted.budget.usage.cost_microunits <= 2_266_080
    assert persisted.budget.usage.tokens <= 147_456
    assert persisted.budget.usage.tool_calls <= 15
