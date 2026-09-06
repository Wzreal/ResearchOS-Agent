"""Offline Phase 11 portfolio E2E through the production REAL composition path."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from researchos.adapters.checkpoint_filesystem import FilesystemCheckpointStore
from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.deepseek import _messages
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.filesystem import FilesystemRunStore, FilesystemTraceSink
from researchos.adapters.openai_compatible import OpenAICompatibleChatTransport
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.real_composition import BoundRealModel
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
_DYNAMIC_TASK_IDS = (
    "task_official_mission_search",
    "task_press_release_search",
    "task_verification",
    "task_evidence_synthesis",
    "task_final_report",
)
_EXPECTED_REQUEST_BYTES = {
    "planning": 5_378,
    "agent:task_official_mission_search:step1": 7_550,
    "agent:task_official_mission_search:step2": 8_583,
    "agent:task_press_release_search:step1": 7_575,
    "agent:task_press_release_search:step2": 8_605,
    "agent:task_verification:step1": 7_516,
    "agent:task_verification:step2": 8_538,
    "claim_extraction": 3_603,
    "verification:synthesizer:round0": 9_466,
    "verification:red:round1": 11_114,
    "verification:blue:round1": 11_115,
    "verification:judge:round1": 11_186,
}


def _settings(
    max_input_tokens: int = 8_192, max_output_tokens: int = 4_096
):
    values = {
        "RESEARCHOS_MAX_INPUT_TOKENS": str(max_input_tokens),
        "RESEARCHOS_MAX_OUTPUT_TOKENS": str(max_output_tokens),
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


class _RequestMeter:
    """Exercise the exact production JSON request admission construction offline."""

    def __init__(self) -> None:
        self._settings = _settings(1_000_000)
        self.measurements: list[tuple[str, str, int]] = []

    def record(self, role_id: str, request) -> None:
        settings = self._settings.model_for_role(role_id)
        if hasattr(request, "run_id"):
            run_id = request.run_id
        elif hasattr(request.context, "run_id"):
            run_id = request.context.run_id
        else:
            run_id = "run_offline_meter"
        bound = BoundRealModel(
            run_id=run_id,
            run_config_hash="a" * 64,
            composition_hash="b" * 64,
            settings=settings,
            bundle=settings.bundle(),
            credential="offline-test-secret",
        )
        transport = OpenAICompatibleChatTransport(
            bound, sync_client=object(), async_client=object()
        )
        messages = _messages(
            role_id,
            request,
            enable_web_tools=role_id == "agent",
            planning_model_id=(settings.model_id if role_id == "planning" else None),
        )
        encoded = transport._request_bytes(messages)
        label = role_id
        if role_id == "agent":
            label = f"agent:{request.context.task_id}:step{request.agent_step}"
        elif role_id == "verification":
            label = f"verification:{request.role.value}:round{request.round_number}"
        self.measurements.append((role_id, label, len(encoded)))


def _portfolio_profile(settings):
    profile = build_phase10_mock_bundle_v1().workflow_profile
    provider = settings.model_for_role("planning").policy.provider_call_reservation
    assert all(
        settings.model_for_role(role).policy.provider_call_reservation.total_tokens
        == provider.total_tokens
        and (
            settings.model_for_role(role)
            .policy.provider_call_reservation.cost_microunits
        == provider.cost_microunits
        )
        for role in ("agent", "claim_extraction", "verification")
    )
    tavily_cost = (
        settings.capability_for_id("web_search").provider_reservation.cost_microunits
    )
    allocation = WorkflowBudgetAllocation(
        total=RuntimeResourceAmount(
            duration_milliseconds=1_080_000,
            tokens=provider.total_tokens * 12,
            cost_microunits=provider.cost_microunits * 12 + tavily_cost * 3,
            tool_calls=15,
        ),
        planning=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=provider.total_tokens,
            cost_microunits=provider.cost_microunits,
            tool_calls=1,
        ),
        execution=RuntimeResourceAmount(
            duration_milliseconds=540_000,
            tokens=provider.total_tokens * 6,
            cost_microunits=provider.cost_microunits * 6 + tavily_cost * 3,
            tool_calls=9,
        ),
        claim_extraction=RuntimeResourceAmount(
            duration_milliseconds=90_000,
            tokens=provider.total_tokens,
            cost_microunits=provider.cost_microunits,
            tool_calls=1,
        ),
        verification=RuntimeResourceAmount(
            duration_milliseconds=360_000,
            tokens=provider.total_tokens * 4,
            cost_microunits=provider.cost_microunits * 4,
            tool_calls=4,
        ),
    )
    template = profile.execution.task_policies[0].model_copy(
        update={
            "timeout_milliseconds": 180_000,
            "reservation": RuntimeResourceAmount(
                duration_milliseconds=180_000,
                tokens=provider.total_tokens * 2,
                cost_microunits=provider.cost_microunits * 2 + tavily_cost,
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
    profile = _portfolio_profile(settings)
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
    def __init__(self, meter: _RequestMeter) -> None:
        self._meter = meter
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        self._meter.record("planning", request)
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
    def __init__(self, reservation, meter: _RequestMeter) -> None:
        self.provider_call_reservation = reservation
        self.provider_admission_profile = (
            ProviderAdmissionProfile.ACCUMULATED_REMAINING_V1
        )
        self._meter = meter
        self.requests = []

    async def decide(self, request, _cancellation):
        self.requests.append(request)
        self._meter.record("agent", request)
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


class _FakeClaimExtraction:
    def __init__(self, meter: _RequestMeter) -> None:
        self._meter = meter
        self._delegate = Phase10MockClaimExtractionModel()

    @property
    def model_bundle_hash(self) -> str:
        return self._delegate.model_bundle_hash

    @property
    def invocation_count(self) -> int:
        return self._delegate.invocation_count

    def generate(self, request):
        self._meter.record("claim_extraction", request)
        return self._delegate.generate(request)


class _FakeVerification:
    def __init__(self, meter: _RequestMeter) -> None:
        self._delegate = Phase10MockVerificationModel()
        self._meter = meter
        self.requests = []

    async def invoke(self, request, cancellation):
        self.requests.append(request)
        self._meter.record("verification", request)
        response = await self._delegate.invoke(request, cancellation)
        return response.model_copy(update={"mode": "real"})


class _FakeIntegrations:
    def __init__(self, settings, meter: _RequestMeter) -> None:
        self.planning = _FakePlanning(meter)
        self.agent_adapter = _FakeAgent(
            settings.model_for_role("agent").suboperation_reservation(), meter
        )
        capability = settings.capability_for_id("web_search")
        self.search = _FakeWebSearch(
            capability.descriptor(), capability.provider_reservation
        )
        self.claim_extraction = _FakeClaimExtraction(meter)
        self.verification = _FakeVerification(meter)
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


def test_phase11_portfolio_real_composition_completes_offline_dynamic_five_task_case(
    tmp_path,
) -> None:
    settings = _settings(32_768, 8_192)
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
    meter = _RequestMeter()
    fake = _FakeIntegrations(settings, meter)
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
    assert len(fake.agent_adapter.requests) == 10
    assert len(fake.search.invocations) == 5
    assert fake.claim_extraction.invocation_count == 1
    assert len(fake.verification.requests) == 4
    assert {role for role, _, _ in meter.measurements} == {
        "planning",
        "agent",
        "claim_extraction",
        "verification",
    }
    measurements = {label: size for _, label, size in meter.measurements}
    assert all(size <= 32_768 for size in measurements.values())
    trace = FilesystemTraceSink(tmp_path).read(state.run_id)
    assert TraceEventType.PLANNING_VALIDATED in {item.event_type for item in trace}
    assert sum(item.event_type is TraceEventType.AGENT_COMPLETED for item in trace) == 5
    tool_completed = sum(
        item.event_type is TraceEventType.TOOL_INVOCATION_SUCCEEDED for item in trace
    )
    assert tool_completed == 5
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
    assert persisted.budget.usage.cost_microunits <= 6_198_240
    assert persisted.budget.usage.tokens <= 491_520
    assert persisted.budget.usage.tool_calls <= 15


def test_phase11_portfolio_input_cap_candidates_cover_measured_requests() -> None:
    first_failing_request = {
        cap: next(
            (
                label
                for label, size in _EXPECTED_REQUEST_BYTES.items()
                if size > cap
            ),
            None,
        )
        for cap in (8_192, 12_288, 16_384, 24_576, 32_768)
    }
    assert first_failing_request == {
        8_192: "agent:task_official_mission_search:step2",
        12_288: None,
        16_384: None,
        24_576: None,
        32_768: None,
    }
