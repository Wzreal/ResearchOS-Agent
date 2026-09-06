from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from researchos import cli
from researchos.adapters.openai_compatible import OpenAICompatibleChatTransport
from researchos.application.errors import RealProviderFailure
from researchos.application.real_composition import BoundRealModel
from researchos.application.workflow_evaluation import structural_selfcheck_request
from researchos.application.workflow_factory import WorkflowFactory
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1
from researchos.configuration.phase11 import (
    Phase11BatchCapacity,
    Phase11RealBenchmarkBundleV1,
    phase11_evaluation_policy,
    phase11_real_run_config,
)
from researchos.configuration.phase11_benchmark import build_phase11_real_benchmark_v1
from researchos.configuration.validation import (
    PHASE11_AGENT_RESPONSE_CONTRACT,
    Phase11RealAgentDecision,
    deepseek_system_prompt,
)
from researchos.domain.contracts import (
    BudgetLimits,
    RunInput,
    RunStatus,
    TraceEventType,
    model_sha256,
)
from researchos.domain.real_tools import EvidenceGapCategory
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.verification_runtime import max_model_calls


def _settings():
    values = {
        "RESEARCHOS_MAX_INPUT_TOKENS": "32768",
        "RESEARCHOS_MAX_OUTPUT_TOKENS": "4096",
        "RESEARCHOS_MAX_REQUEST_BYTES": "1048576",
        "RESEARCHOS_MAX_RESPONSE_BYTES": "1048576",
        "RESEARCHOS_TEMPERATURE": "0",
        "RESEARCHOS_TOP_P": "1",
        "RESEARCHOS_CONNECT_TIMEOUT_MS": "10000",
        "RESEARCHOS_READ_TIMEOUT_MS": "60000",
        "RESEARCHOS_WRITE_TIMEOUT_MS": "10000",
        "RESEARCHOS_POOL_TIMEOUT_MS": "10000",
        "RESEARCHOS_COST_CURRENCY": "USD",
        "RESEARCHOS_MAX_COST_MICROUNITS_PER_CALL": "11000000",
        "RESEARCHOS_PRICING_POLICY_VERSION": "v1",
        "RESEARCHOS_INPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "10000000",
        "RESEARCHOS_OUTPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "20000000",
        "RESEARCHOS_DEEPSEEK_BASE_ENDPOINT": "https://api.deepseek.com",
        "RESEARCHOS_DEEPSEEK_ADAPTER_VERSION": "v1",
        "RESEARCHOS_REAL_CAPABILITIES": "web_browser,web_search",
        "RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS": "90000",
        "RESEARCHOS_PHASE11_PLANNING_TOTAL_CALL_TIMEOUT_MS": "37",
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


def _bundle(settings):
    source = build_phase10_mock_bundle_v1().workflow_profile
    profile = source.model_copy(
        update={
            "planning_policy": source.planning_policy.model_copy(
                update={"require_explicit_web_search_capability": True}
            )
        }
    )
    return Phase11RealBenchmarkBundleV1(
        bundle_id="phase11_real_benchmark",
        bundle_version="1",
        workflow_profile=profile,
        benchmark=build_phase11_real_benchmark_v1(),
        evaluation_policy=phase11_evaluation_policy(profile),
        real_settings_hash=model_sha256(settings),
        capability_ids=tuple(
            item.capability_id for item in settings.capability_settings
        ),
        approved_refs=("main",),
        max_agent_steps=2,
        max_agent_tool_calls=1,
        system_commit_sha=profile.system_commit_sha,
        system_version=profile.system_version,
        max_batch_cost_microunits=50_000_000,
    )


def test_build_real_constructs_without_network_or_credentials(tmp_path) -> None:
    settings = _settings()
    coordinator = WorkflowFactory(tmp_path).build_real(
        bundle=_bundle(settings), settings=settings, secrets=EnvironmentSecretSource({})
    )
    assert coordinator is not None
    assert (
        coordinator._planning_reservation
        == settings.model_for_role("planning").suboperation_reservation()
    )
    assert coordinator._planning_reservation.duration_milliseconds == 37
    assert not list(tmp_path.rglob("real_composition.json"))


def test_phase11_web_search_requires_bounded_evidence_gap_before_dispatch() -> None:
    settings = _settings()
    agent = settings.model_for_role("agent")
    assert agent.response_contract_version == PHASE11_AGENT_RESPONSE_CONTRACT
    prompt = deepseek_system_prompt(
        "agent",
        enable_web_tools=True,
        web_search_max_results=5,
        require_evidence_gap_for_web_search=True,
    )
    assert "collected observations" in prompt
    assert "return a final decision" in prompt
    assert "Before each web_search or web_browser" in prompt
    assert "do NOT request another web_search or web_browser" in prompt
    assert "Search-provider metadata and snippets are discovery-only" in prompt
    assert "do not return FINAL until a successful browser fetch" in prompt
    gapless = {
        "kind": "tool_call",
        "tool_call": {
            "tool_call_id": "search_more",
            "capability_id": "web_search",
            "input": {"input_type": "search", "query": "more", "limit": 5},
        },
        "usage": RuntimeResourceAmount().model_dump(mode="json"),
        "usage_certainty": UsageCertainty.EXACT.value,
    }
    with pytest.raises(ValidationError):
        TypeAdapter(Phase11RealAgentDecision).validate_python(gapless)
    admitted = TypeAdapter(Phase11RealAgentDecision).validate_python(
        {
            **gapless,
            "evidence_status": "insufficient",
            "remaining_evidence_gap": EvidenceGapCategory.CORROBORATING_SOURCE_MISSING,
        }
    )
    assert admitted.evidence_status == "insufficient"


def test_phase11_browser_stage_is_bound_by_the_evidence_gap_contract() -> None:
    """The v4 contract governs web_browser like web_search (final-19 gap)."""
    legacy_prompt = deepseek_system_prompt(
        "agent",
        enable_web_tools=True,
        web_search_max_results=5,
        require_evidence_gap_for_web_search=False,
    )
    assert "Before each web_search or web_browser" not in legacy_prompt
    assert "do NOT request another web_search or web_browser" not in legacy_prompt
    gapless_browser = {
        "kind": "tool_call",
        "tool_call": {
            "tool_call_id": "browse_more",
            "capability_id": "web_browser",
            "input": {"input_type": "browser", "url": "https://example.com/"},
        },
        "usage": RuntimeResourceAmount().model_dump(mode="json"),
        "usage_certainty": UsageCertainty.EXACT.value,
    }
    with pytest.raises(ValidationError):
        TypeAdapter(Phase11RealAgentDecision).validate_python(gapless_browser)
    admitted = TypeAdapter(Phase11RealAgentDecision).validate_python(
        {
            **gapless_browser,
            "evidence_status": "insufficient",
            "remaining_evidence_gap": EvidenceGapCategory.AUTHORITATIVE_SOURCE_MISSING,
        }
    )
    assert admitted.tool_call.capability_id == "web_browser"
    assert admitted.remaining_evidence_gap == (
        EvidenceGapCategory.AUTHORITATIVE_SOURCE_MISSING
    )


def test_phase11_planning_prompt_requires_exact_web_capabilities() -> None:
    phase11_prompt = deepseek_system_prompt(
        "planning", require_explicit_web_search_capability=True
    )
    legacy_prompt = deepseek_system_prompt("planning")

    assert '"web_search"' in phase11_prompt
    assert '"web_browser"' in phase11_prompt
    assert "exact subset of allowed_capability_ids" in phase11_prompt
    assert "exact subset of allowed_capability_ids" not in legacy_prompt


def test_phase11_input_admission_reserves_the_enforced_input_cap() -> None:
    settings = _settings()
    planning = settings.model_for_role("planning")
    profile = planning.policy.pricing_safety_profile
    reservation = planning.policy.provider_call_reservation
    assert profile.context_window_tokens == 1_000_000
    assert planning.policy.max_input_tokens == 32_768
    assert planning.policy.input_reservation_basis == "enforced_utf8_input_limit_v1"
    assert reservation.input_tokens == 32_768
    assert reservation.output_tokens == 4_096
    assert reservation.cost_microunits == (
        32_768
        * planning.policy.input_cost_upper_bound_microunits_per_million_tokens
        + 4_096
        * planning.policy.output_cost_upper_bound_microunits_per_million_tokens
        + 999_999
    ) // 1_000_000


def test_phase11_single_case_reservation_fits_frozen_cost_ceiling() -> None:
    settings = _settings()
    bundle = _bundle(settings)
    per_call = settings.model_for_role("planning").policy.provider_call_reservation
    task_count = len(bundle.workflow_profile.execution.task_policies)
    agent_calls = task_count * bundle.max_agent_steps
    tavily_calls = task_count * bundle.max_agent_tool_calls
    verification_calls = max_model_calls(bundle.workflow_profile.verification_policy)
    case_reservation = (
        per_call.cost_microunits
        * (1 + agent_calls + 1 + verification_calls)
        + tavily_calls
        * settings.capability_settings[0].provider_reservation.cost_microunits
    )
    capacity = Phase11BatchCapacity(bundle.max_batch_cost_microunits)
    capacity.reserve(case_reservation)
    assert case_reservation < bundle.max_batch_cost_microunits
    assert capacity.remaining_microunits == (
        bundle.max_batch_cost_microunits - case_reservation
    )


def test_build_real_rejects_tampered_settings_pin(tmp_path) -> None:
    settings = _settings()
    bundle = _bundle(settings).model_copy(update={"real_settings_hash": "0" * 64})
    with pytest.raises(ValueError, match="settings differ"):
        WorkflowFactory(tmp_path).build_real(
            bundle=bundle, settings=settings, secrets=EnvironmentSecretSource({})
        )


class _BlockingResponse:
    status_code = 200

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        self.started.set()
        self.release.wait(timeout=1)
        yield b"{}"


class _BlockingSyncClient:
    def __init__(self, response: _BlockingResponse) -> None:
        self.response = response
        self.calls: list[object] = []

    def stream(self, *_args, **_kwargs):
        self.calls.append(object())
        return self.response


def test_phase11_planning_total_timeout_bounds_entire_sync_dispatch() -> None:
    settings = _settings()
    model = settings.model_for_role("planning")
    assert model.suboperation_reservation().duration_milliseconds == 37
    bound = BoundRealModel(
        run_id="run_timeout",
        run_config_hash="a" * 64,
        composition_hash="b" * 64,
        settings=model,
        bundle=model.bundle(),
        credential="offline-canary",
    )
    response = _BlockingResponse()
    client = _BlockingSyncClient(response)
    transport = OpenAICompatibleChatTransport(bound, sync_client=client)
    started = time.monotonic()
    with pytest.raises(
        RealProviderFailure, match="provider_total_call_timeout"
    ) as caught:
        transport.complete(({"role": "user", "content": "offline"},))
    assert time.monotonic() - started < 0.5
    assert response.started.is_set()
    assert caught.value.diagnostic.value == "dispatched_outcome_unknown"
    assert caught.value.retryable is False
    assert len(client.calls) == 1
    response.release.set()


def test_legacy_planning_settings_do_not_gain_phase11_total_timeout() -> None:
    environment = {
        "RESEARCHOS_MAX_INPUT_TOKENS": "32768",
        "RESEARCHOS_MAX_OUTPUT_TOKENS": "4096",
        "RESEARCHOS_MAX_REQUEST_BYTES": "1048576",
        "RESEARCHOS_MAX_RESPONSE_BYTES": "1048576",
        "RESEARCHOS_TEMPERATURE": "0",
        "RESEARCHOS_TOP_P": "1",
        "RESEARCHOS_CONNECT_TIMEOUT_MS": "10000",
        "RESEARCHOS_READ_TIMEOUT_MS": "60000",
        "RESEARCHOS_WRITE_TIMEOUT_MS": "10000",
        "RESEARCHOS_POOL_TIMEOUT_MS": "10000",
        "RESEARCHOS_COST_CURRENCY": "USD",
        "RESEARCHOS_MAX_COST_MICROUNITS_PER_CALL": "11000000",
        "RESEARCHOS_PRICING_POLICY_VERSION": "v1",
        "RESEARCHOS_INPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "10000000",
        "RESEARCHOS_OUTPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS": "20000000",
        "RESEARCHOS_DEEPSEEK_BASE_ENDPOINT": "https://api.deepseek.com",
        "RESEARCHOS_DEEPSEEK_ADAPTER_VERSION": "v1",
    }
    for role, model_id in {
        "PLANNING": "deepseek-v4-pro",
        "AGENT": "deepseek-v4-flash",
        "VERIFICATION": "deepseek-v4-pro",
    }.items():
        environment[f"RESEARCHOS_DEEPSEEK_{role}_MODEL"] = model_id
        environment[f"RESEARCHOS_DEEPSEEK_{role}_THINKING_MODE"] = "enabled"
        environment[f"RESEARCHOS_DEEPSEEK_{role}_REASONING_EFFORT"] = "high"
    legacy = load_real_integration_settings(environment)
    planning = legacy.model_for_role("planning")
    assert planning.phase11_planning_total_timeout_enabled is False
    assert planning.policy.provider_total_call_timeout_ms is None
    assert planning.policy.input_reservation_basis == "provider_context_window_v1"
    assert planning.policy.provider_call_reservation.input_tokens == 1_000_000


def test_phase11_real_evaluating_fresh_process_rebind_reuses_evaluation_without_resumed_trace(  # noqa: E501
    tmp_path,
) -> None:
    settings = _settings()
    bundle = _bundle(settings)
    secrets = EnvironmentSecretSource(
        {
            "researchos_deepseek_api_key": "offline-only",
            "researchos_tavily_api_key": "offline-only",
        }
    )
    original = WorkflowFactory(tmp_path).build_real(
        bundle=bundle, settings=settings, secrets=secrets
    )
    config = phase11_real_run_config(bundle, settings).model_copy(
        update={
            "budget_limits": BudgetLimits(
                max_duration_seconds=600,
                max_tokens=2_000_000,
                max_cost_microunits=50_000_000,
                max_tool_calls=10,
            )
        }
    )
    state = original._runs.create(
        RunInput(query="offline evaluating recovery"),
        config,
    )
    for status in (
        RunStatus.PLANNING,
        RunStatus.READY,
        RunStatus.RUNNING,
        RunStatus.VERIFYING,
        RunStatus.EVALUATING,
    ):
        state = original._runs.transition(state.run_id, status)
    request = structural_selfcheck_request(
        state, bundle.workflow_profile, clock=original._clock
    )
    asyncio.run(
        original._evaluation_harness(state).evaluate_existing(
            request, original._cancellation
        )
    )
    evaluation_files_before = tuple(sorted((tmp_path / "evaluations").rglob("*")))
    trace_before = original._trace.read(state.run_id, recover_torn_tail=True)
    del original

    recovered = WorkflowFactory(tmp_path).build_real(
        bundle=bundle, settings=settings, secrets=secrets
    )
    result = asyncio.run(
        recovered.resume_and_execute(
            state.run_id,
            expected_input=RunInput(query="offline evaluating recovery"),
            expected_config=config,
        )
    )
    evaluation_files_after = tuple(sorted((tmp_path / "evaluations").rglob("*")))
    trace_after = recovered._trace.read(state.run_id, recover_torn_tail=True)

    assert result.status is RunStatus.COMPLETED
    assert evaluation_files_after == evaluation_files_before
    assert [item.event_type for item in trace_after] == [
        item.event_type for item in trace_before
    ] + [TraceEventType.TRANSITION_INTENT, TraceEventType.TRANSITION_COMMITTED]
    assert TraceEventType.RESUMED not in {item.event_type for item in trace_after}


def _offline_benchmark_runner(monkeypatch, tmp_path):
    settings = _settings()
    bundle = _bundle(settings)
    bundle_path = tmp_path / "phase11-bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    queries: list[str] = []
    evaluations: list[tuple[str, str, str]] = []

    class Coordinator:
        async def create_and_execute(self, run_input, config):
            del config
            queries.append(run_input.query)
            return SimpleNamespace(
                run_id=f"run_{len(queries)}", status=RunStatus.COMPLETED
            )

    class Factory:
        def __init__(self, root):
            del root

        def build_real(self, *, bundle, settings, secrets):
            del settings, secrets
            assert bundle.benchmark.dataset_id == "phase11_real_benchmark"
            assert bundle.benchmark.dataset_version == "1"
            return Coordinator()

        async def evaluate_phase11(self, *, bundle, run_id, case_id, cancellation):
            del cancellation
            case = next(
                item for item in bundle.benchmark.cases if item.case_id == case_id
            )
            evaluations.append((run_id, case_id, case.query))
            return SimpleNamespace(eval_run_id=f"eval_{case_id}")

    def git_output(*args):
        if args == ("rev-parse", "HEAD"):
            return bundle.system_commit_sha
        if args == ("status", "--porcelain"):
            return ""
        if args == ("branch", "--show-current"):
            return "main"
        raise AssertionError(args)

    monkeypatch.setattr(cli, "WorkflowFactory", Factory)
    monkeypatch.setattr(
        cli, "load_real_integration_settings", lambda **_kwargs: settings
    )
    monkeypatch.setattr(cli, "_git_output", git_output)
    return bundle, bundle_path, queries, evaluations


def test_phase11_benchmark_case_id_runs_one_formal_case(monkeypatch, tmp_path) -> None:
    bundle, bundle_path, queries, evaluations = _offline_benchmark_runner(
        monkeypatch, tmp_path
    )
    result = cli._benchmark_real(
        tmp_path,
        bundle_path=bundle_path,
        expected_commit_sha=bundle.system_commit_sha,
        approved_cost_microunits=1_000,
        acknowledge_real=True,
        case_id="p11_citation_chain",
    )
    selected = next(
        item for item in bundle.benchmark.cases if item.case_id == "p11_citation_chain"
    )
    assert tuple(result) == ("p11_citation_chain",)
    assert queries == [selected.query]
    assert evaluations == [("run_1", selected.case_id, selected.query)]


def test_phase11_benchmark_case_id_rejects_unknown_before_real_composition(
    monkeypatch, tmp_path
) -> None:
    bundle, bundle_path, queries, evaluations = _offline_benchmark_runner(
        monkeypatch, tmp_path
    )
    monkeypatch.setattr(
        cli,
        "load_real_integration_settings",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("settings loaded")),
    )
    with pytest.raises(ValueError, match="case ID is unknown"):
        cli._benchmark_real(
            tmp_path,
            bundle_path=bundle_path,
            expected_commit_sha=bundle.system_commit_sha,
            approved_cost_microunits=1_000,
            acknowledge_real=True,
            case_id="unknown_case",
        )
    assert queries == []
    assert evaluations == []


def test_phase11_benchmark_without_case_id_preserves_all_twelve_cases(
    monkeypatch, tmp_path
) -> None:
    bundle, bundle_path, queries, evaluations = _offline_benchmark_runner(
        monkeypatch, tmp_path
    )
    result = cli._benchmark_real(
        tmp_path,
        bundle_path=bundle_path,
        expected_commit_sha=bundle.system_commit_sha,
        approved_cost_microunits=12_000,
        acknowledge_real=True,
    )
    assert tuple(result) == tuple(item.case_id for item in bundle.benchmark.cases)
    assert len(queries) == len(evaluations) == 12
