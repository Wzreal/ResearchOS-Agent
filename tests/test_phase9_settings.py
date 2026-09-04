from __future__ import annotations

import os
import subprocess
import sys

import pytest
from phase9_fixtures import make_settings

import researchos.application.doctor as doctor_module
from researchos.application.doctor import DoctorCheckStatus, RealDoctor
from researchos.configuration.environment import (
    EnvironmentSecretSource,
    load_real_integration_settings,
)
from researchos.configuration.real_settings import (
    DEEPSEEK_PRICING_SAFETY_PROFILE_VERSION,
    base_endpoint_hash,
    canonicalize_base_endpoint,
    deepseek_pricing_safety_profile,
    default_deepseek_policy,
)
from researchos.configuration.validation import (
    deepseek_prompt_content_hash,
    deepseek_response_contract,
)
from researchos.domain.identity import stable_hash
from researchos.domain.real_composition import DeepSeekPricingUpperBoundProfile


def _environment() -> dict[str, str]:
    return {
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
        "RESEARCHOS_DEEPSEEK_PLANNING_MODEL": "deepseek-v4-pro",
        "RESEARCHOS_DEEPSEEK_AGENT_MODEL": "deepseek-v4-flash",
        "RESEARCHOS_DEEPSEEK_VERIFICATION_MODEL": "deepseek-v4-pro",
        "RESEARCHOS_DEEPSEEK_PLANNING_THINKING_MODE": "enabled",
        "RESEARCHOS_DEEPSEEK_AGENT_THINKING_MODE": "enabled",
        "RESEARCHOS_DEEPSEEK_VERIFICATION_THINKING_MODE": "enabled",
        "RESEARCHOS_DEEPSEEK_PLANNING_REASONING_EFFORT": "high",
        "RESEARCHOS_DEEPSEEK_AGENT_REASONING_EFFORT": "high",
        "RESEARCHOS_DEEPSEEK_VERIFICATION_REASONING_EFFORT": "high",
    }


def _managed_retrieval_environment() -> dict[str, str]:
    environment = _environment()
    environment.update(
        {
            "RESEARCHOS_REAL_CAPABILITIES": "managed_retrieval",
            "RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS": "90000",
            "RESEARCHOS_ZILLIZ_ENDPOINT": (
                "https://cluster-a.serverless.aws-us-east-1.vectordb.zillizcloud.com/"
            ),
            "RESEARCHOS_ZILLIZ_COLLECTION_ID": "research_docs",
            "RESEARCHOS_ZILLIZ_FREE_PLAN_AUTHORITY_ID": (
                "operator_provisioned_free_plan_v1"
            ),
        }
    )
    return environment


def test_canonical_base_endpoint_includes_semantic_path() -> None:
    assert base_endpoint_hash("https://API.DeepSeek.com:443/v1/") == (
        base_endpoint_hash("https://api.deepseek.com/v1")
    )
    assert base_endpoint_hash("https://api.deepseek.com/v1") != (
        base_endpoint_hash("https://api.deepseek.com/v2")
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://api.deepseek.com/v1",
        "https://user:password@api.deepseek.com/v1",
        "https://api.deepseek.com/v1?key=value",
        "https://api.deepseek.com/v1#fragment",
        "https://api.deepseek.com/a/../v1",
    ],
)
def test_unsafe_or_ambiguous_base_endpoint_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_base_endpoint(value)


def test_deepseek_http_endpoint_is_rejected_before_transport_construction() -> None:
    provider_calls: list[object] = []
    with pytest.raises(ValueError, match="must use HTTPS"):
        make_settings(
            role_overrides={
                "planning": {"base_endpoint": "http://api.deepseek.com/v1"}
            }
        )
    assert provider_calls == []


def test_credential_values_are_not_part_of_settings_or_identity() -> None:
    settings = make_settings()
    encoded = settings.model_dump_json()
    assert "credential-one" not in encoded
    first = EnvironmentSecretSource(
        {"RESEARCHOS_DEEPSEEK_API_KEY": "credential-one"}
    )
    second = EnvironmentSecretSource(
        {"RESEARCHOS_DEEPSEEK_API_KEY": "credential-two"}
    )
    assert first.get_secret("researchos_deepseek_api_key") != second.get_secret(
        "researchos_deepseek_api_key"
    )
    assert make_settings() == settings


@pytest.mark.parametrize("value", ["", " ", "\t\r\n"])
def test_secret_source_rejects_empty_or_whitespace_credentials(value: str) -> None:
    source = EnvironmentSecretSource({"RESEARCHOS_DEEPSEEK_API_KEY": value})
    assert source.get_secret("researchos_deepseek_api_key") is None


def test_environment_loader_resolves_complete_effective_settings() -> None:
    settings = load_real_integration_settings(_environment())
    assert tuple(item.role_id for item in settings.models) == (
        "agent",
        "planning",
        "verification",
    )
    assert all(item.policy.read_timeout_ms == 60_000 for item in settings.models)
    assert settings.model_for_role("planning").model_id == "deepseek-v4-pro"
    assert settings.model_for_role("agent").model_id == "deepseek-v4-flash"
    assert all(
        item.policy.provider_request_policy.thinking_mode.value == "enabled"
        and item.policy.provider_request_policy.reasoning_effort.value == "high"
        for item in settings.models
    )
    assert all(
        item.credential_slot_id == "researchos_deepseek_api_key"
        for item in settings.models
    )
    assert "credential-one" not in settings.model_dump_json()


def test_environment_loader_resolves_exact_phase9b_capabilities() -> None:
    environment = _environment()
    environment["RESEARCHOS_REAL_CAPABILITIES"] = "web_search,web_browser"
    environment["RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS"] = "90000"
    environment["RESEARCHOS_TAVILY_MICROUNITS_PER_CREDIT"] = "125000"
    settings = load_real_integration_settings(environment)
    assert tuple(item.capability_id for item in settings.capability_settings) == (
        "web_browser",
        "web_search",
    )
    search = settings.capability_for_id("web_search")
    assert search.provider_reservation.cost_microunits == 125_000
    assert search.pin() in settings.capabilities
    agent = settings.model_for_role("agent")
    assert agent.response_contract_version == "agent-tool-decision-v2"


def test_environment_loader_constructs_attested_managed_retrieval_v3() -> None:
    settings = load_real_integration_settings(_managed_retrieval_environment())
    retrieval = settings.capability_for_id("managed_retrieval")
    assert retrieval.retrieval_policy is not None
    assert retrieval.retrieval_policy.free_plan_attestation.authority_id == (
        "operator_provisioned_free_plan_v1"
    )
    assert settings.model_for_role("agent").response_contract_version == (
        "agent-tool-decision-v3"
    )
    assert settings.model_for_role("agent").prompt_content_hash == (
        deepseek_prompt_content_hash("agent", enable_retrieval_tools=True)
    )
    assert settings.model_for_role("agent").response_contract_version == (
        deepseek_response_contract("agent", enable_retrieval_tools=True)
    )


@pytest.mark.parametrize(
    "key",
    [
        "RESEARCHOS_ZILLIZ_ENDPOINT",
        "RESEARCHOS_ZILLIZ_COLLECTION_ID",
        "RESEARCHOS_ZILLIZ_FREE_PLAN_AUTHORITY_ID",
    ],
)
def test_managed_retrieval_environment_fails_closed_without_required_setting(
    key: str,
) -> None:
    environment = _managed_retrieval_environment()
    del environment[key]
    with pytest.raises(ValueError, match=f"required REAL setting is absent: {key}"):
        load_real_integration_settings(environment)


def test_environment_loader_without_real_tools_keeps_agent_v1() -> None:
    settings = load_real_integration_settings(_environment())
    assert settings.model_for_role("agent").response_contract_version == (
        "agent-direct-decision-v1"
    )


def test_doctor_checks_zilliz_credential_and_pymilvus_for_managed_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_modules: list[str] = []

    def find_spec(name: str):
        checked_modules.append(name)
        return object()

    monkeypatch.setattr(doctor_module, "find_spec", find_spec)
    class RecordingSecrets(EnvironmentSecretSource):
        def __init__(self) -> None:
            super().__init__(
                {
                    "RESEARCHOS_DEEPSEEK_API_KEY": "deepseek-token",
                    "RESEARCHOS_ZILLIZ_TOKEN": "z-token",
                }
            )
            self.lookups: list[str] = []

        def get_secret(self, secret_id: str) -> str | None:
            self.lookups.append(secret_id)
            return super().get_secret(secret_id)

    secrets = RecordingSecrets()
    report = RealDoctor(
        settings=load_real_integration_settings(_managed_retrieval_environment()),
        secrets=secrets,
    ).run()
    assert "pymilvus" in checked_modules
    assert next(
        check for check in report.checks if check.check_id == "secret_presence"
    ).status is DoctorCheckStatus.PASS
    assert "researchos_zilliz_token" in secrets.lookups


def test_environment_loader_rejects_unknown_real_capability() -> None:
    environment = _environment()
    environment["RESEARCHOS_REAL_CAPABILITIES"] = "python"
    with pytest.raises(ValueError, match="unsupported REAL capability"):
        load_real_integration_settings(environment)


def test_ignored_sampling_environment_values_do_not_change_identity() -> None:
    baseline = load_real_integration_settings(_environment())
    changed_environment = _environment()
    changed_environment["RESEARCHOS_TEMPERATURE"] = "1.7"
    changed_environment["RESEARCHOS_TOP_P"] = "0.2"
    changed = load_real_integration_settings(changed_environment)
    assert changed == baseline


def test_environment_loader_fails_closed_on_missing_setting() -> None:
    environment = _environment()
    del environment["RESEARCHOS_DEEPSEEK_AGENT_MODEL"]
    with pytest.raises(ValueError, match="required REAL setting is absent"):
        load_real_integration_settings(environment)


@pytest.mark.parametrize("model_id", ["deepseek-v4-flash", "deepseek-v4-pro"])
def test_adapter_has_versioned_pricing_safety_profile(model_id: str) -> None:
    profile = deepseek_pricing_safety_profile(model_id)
    assert profile.model_id == model_id
    assert profile.profile_version == DEEPSEEK_PRICING_SAFETY_PROFILE_VERSION
    assert profile.context_window_tokens == 1_000_000
    assert profile.provider_max_output_tokens == 384_000
    assert profile.billing_currency == "USD"
    assert profile.profile_hash == profile.compute_hash()


def test_unknown_model_has_no_implicit_pricing_safety_profile() -> None:
    with pytest.raises(ValueError, match="unsupported DeepSeek pricing safety model"):
        default_deepseek_policy(model_id="deepseek-future-alias")


@pytest.mark.parametrize(
    "override",
    [
        {"input_cost_upper_bound_microunits_per_million_tokens": 9_999_999},
        {"output_cost_upper_bound_microunits_per_million_tokens": 19_999_999},
    ],
)
def test_operator_pricing_below_adapter_safety_floor_is_rejected(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="below adapter safety floor"):
        default_deepseek_policy(**override)


def test_operator_may_raise_pricing_upper_bounds() -> None:
    policy = default_deepseek_policy(
        input_cost_upper_bound_microunits_per_million_tokens=11_000_000,
        output_cost_upper_bound_microunits_per_million_tokens=21_000_000,
        max_cost_microunits_per_call=12_000_000,
    )
    assert policy.provider_call_reservation.cost_microunits == 11_086_016


def test_provider_reservation_uses_full_context_not_legacy_131k_bound() -> None:
    policy = default_deepseek_policy(max_input_tokens=131_073)
    assert policy.max_input_tokens == 131_073
    assert policy.provider_call_reservation.input_tokens == 1_000_000
    assert policy.provider_call_reservation.input_tokens > 131_072


@pytest.mark.parametrize("currency", ["CNY", "EUR", "JPY"])
def test_non_usd_policy_currency_is_rejected(currency: str) -> None:
    with pytest.raises(
        ValueError, match="currency differs from pricing safety profile"
    ):
        default_deepseek_policy(cost_currency=currency)


@pytest.mark.parametrize("currency", ["CNY", "EUR", "JPY"])
def test_environment_rejects_non_usd_deepseek_pricing(currency: str) -> None:
    environment = _environment()
    environment["RESEARCHOS_COST_CURRENCY"] = currency
    with pytest.raises(
        ValueError, match="currency differs from pricing safety profile"
    ):
        load_real_integration_settings(environment)


def test_pricing_profile_currency_changes_profile_hash() -> None:
    baseline = deepseek_pricing_safety_profile("deepseek-v4-pro")
    changed = DeepSeekPricingUpperBoundProfile.build(
        model_id=baseline.model_id,
        profile_version=baseline.profile_version,
        context_window_tokens=baseline.context_window_tokens,
        provider_max_output_tokens=baseline.provider_max_output_tokens,
        minimum_safe_input_rate=baseline.minimum_safe_input_rate,
        minimum_safe_output_rate=baseline.minimum_safe_output_rate,
        billing_currency="CNY",
    )
    assert changed.profile_hash != baseline.profile_hash


def test_output_cap_above_provider_model_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="output cap exceeds provider model limit"):
        default_deepseek_policy(
            max_output_tokens=384_001,
            max_cost_microunits_per_call=20_000_000,
        )


def test_pricing_profile_version_changes_policy_identity() -> None:
    baseline = default_deepseek_policy()
    old = baseline.pricing_safety_profile
    changed_profile = DeepSeekPricingUpperBoundProfile.build(
        model_id=old.model_id,
        profile_version="future-profile-v2",
        context_window_tokens=old.context_window_tokens,
        provider_max_output_tokens=old.provider_max_output_tokens,
        minimum_safe_input_rate=old.minimum_safe_input_rate,
        minimum_safe_output_rate=old.minimum_safe_output_rate,
        billing_currency=old.billing_currency,
    )
    values = baseline.model_dump(
        mode="python",
        exclude={
            "model_call_policy_hash",
            "pricing_rules_hash",
            "provider_call_reservation",
        },
    )
    values["pricing_safety_profile"] = changed_profile
    changed = type(baseline).build(**values)
    assert changed.model_call_policy_hash != baseline.model_call_policy_hash


@pytest.mark.parametrize("field", ["temperature", "top_p"])
def test_generation_decimals_reject_noncanonical_aliases(field: str) -> None:
    with pytest.raises(ValueError, match="canonical decimal"):
        default_deepseek_policy(thinking_mode="disabled", **{field: "0.0"})


def test_source_policy_identity_cannot_use_ambiguous_null_hash() -> None:
    settings = make_settings()
    with pytest.raises(ValueError, match="requires its effective hash"):
        type(settings).model_validate(
            {
                **settings.model_dump(mode="json"),
                "source_policy_id": "allowlisted_sources",
                "source_policy_hash": stable_hash(None),
            }
        )


def test_core_package_import_does_not_import_optional_provider_modules() -> None:
    environment = dict(os.environ)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import researchos; "
                "assert 'httpx' not in sys.modules; "
                "assert 'pymilvus' not in sys.modules; "
                "assert 'playwright' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
