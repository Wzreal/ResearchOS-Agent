"""Environment-backed secret source with no persistence behavior."""

from __future__ import annotations

import os
from collections.abc import Mapping

from researchos.configuration.real_settings import (
    RealIntegrationSettings,
    RealModelSettings,
    default_browser_capability,
    default_deepseek_policy,
    default_managed_retrieval_capability,
    default_tavily_capability,
)
from researchos.configuration.validation import (
    deepseek_prompt_content_hash,
    deepseek_response_contract,
)
from researchos.domain.retrieval import ZillizFreePlanAttestation


class EnvironmentSecretSource:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def get_secret(self, secret_id: str) -> str | None:
        value = self._environ.get(secret_id)
        if value is None:
            value = self._environ.get(secret_id.upper())
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


def load_real_integration_settings(
    environ: Mapping[str, str] | None = None,
    *,
    require_phase10_roles: bool = False,
) -> RealIntegrationSettings:
    values = environ if environ is not None else os.environ

    def required(key: str) -> str:
        value = values.get(key)
        if value is None or not value.strip():
            raise ValueError(f"required REAL setting is absent: {key}")
        return value.strip()

    endpoint = required("RESEARCHOS_DEEPSEEK_BASE_ENDPOINT")
    adapter_version = required("RESEARCHOS_DEEPSEEK_ADAPTER_VERSION")
    credential_slot_id = "researchos_deepseek_api_key"
    enabled = tuple(
        sorted(
            item.strip()
            for item in values.get("RESEARCHOS_REAL_CAPABILITIES", "").split(",")
            if item.strip()
        )
    )
    unsupported = set(enabled) - {"managed_retrieval", "web_browser", "web_search"}
    if unsupported:
        raise ValueError("unsupported REAL capability configuration")
    capability_settings = tuple(
        item
        for item in (
            default_browser_capability() if "web_browser" in enabled else None,
            default_tavily_capability(
                microunits_per_credit_upper_bound=int(
                    values.get("RESEARCHOS_TAVILY_MICROUNITS_PER_CREDIT", "100000")
                )
            )
            if "web_search" in enabled
            else None,
            default_managed_retrieval_capability(
                endpoint=required("RESEARCHOS_ZILLIZ_ENDPOINT"),
                collection_id=required("RESEARCHOS_ZILLIZ_COLLECTION_ID"),
                free_plan_attestation=ZillizFreePlanAttestation.operator_provisioned(
                    required("RESEARCHOS_ZILLIZ_FREE_PLAN_AUTHORITY_ID")
                ),
            )
            if "managed_retrieval" in enabled
            else None,
        )
        if item is not None
    )
    capability_settings = tuple(
        sorted(capability_settings, key=lambda item: item.capability_id)
    )
    web_search_max_results = next(
        (
            item.tavily_policy.provider_request.max_results
            for item in capability_settings
            if item.capability_id == "web_search" and item.tavily_policy is not None
        ),
        None,
    )
    enable_web_tools = any(
        item.capability_id in {"web_browser", "web_search"}
        for item in capability_settings
    )
    enable_retrieval_tools = any(
        item.capability_id == "managed_retrieval" for item in capability_settings
    )
    enable_agent_tool_calls = enable_web_tools or enable_retrieval_tools
    models: list[RealModelSettings] = []
    # This already-required explicit Phase 11 marker selects the stricter
    # pre-dispatch UTF-8 admission path.  Its absence preserves Phase 9's
    # context-capacity reservation semantics and frozen artifact hashes.
    phase11_input_admission_enabled = bool(
        values.get("RESEARCHOS_PHASE11_PLANNING_TOTAL_CALL_TIMEOUT_MS")
    )
    roles = ("agent", "planning", "verification")
    if require_phase10_roles:
        roles = ("agent", "planning", "claim_extraction", "verification")
    for role in roles:
        model_id = required(f"RESEARCHOS_DEEPSEEK_{role.upper()}_MODEL")
        thinking_mode = required(f"RESEARCHOS_DEEPSEEK_{role.upper()}_THINKING_MODE")
        policy_overrides: dict[str, object] = {
            "max_input_tokens": int(required("RESEARCHOS_MAX_INPUT_TOKENS")),
            "max_output_tokens": int(required("RESEARCHOS_MAX_OUTPUT_TOKENS")),
            "max_request_bytes": int(required("RESEARCHOS_MAX_REQUEST_BYTES")),
            "max_response_bytes": int(required("RESEARCHOS_MAX_RESPONSE_BYTES")),
            "connect_timeout_ms": int(required("RESEARCHOS_CONNECT_TIMEOUT_MS")),
            "read_timeout_ms": int(required("RESEARCHOS_READ_TIMEOUT_MS")),
            "write_timeout_ms": int(required("RESEARCHOS_WRITE_TIMEOUT_MS")),
            "pool_timeout_ms": int(required("RESEARCHOS_POOL_TIMEOUT_MS")),
            "cost_currency": required("RESEARCHOS_COST_CURRENCY"),
            "max_cost_microunits_per_call": int(
                required("RESEARCHOS_MAX_COST_MICROUNITS_PER_CALL")
            ),
            "pricing_policy_version": required("RESEARCHOS_PRICING_POLICY_VERSION"),
            "input_cost_upper_bound_microunits_per_million_tokens": int(
                required("RESEARCHOS_INPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS")
            ),
            "output_cost_upper_bound_microunits_per_million_tokens": int(
                required("RESEARCHOS_OUTPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS")
            ),
        }
        if phase11_input_admission_enabled:
            policy_overrides["input_reservation_basis"] = (
                "enforced_utf8_input_limit_v1"
            )
        if thinking_mode == "disabled":
            policy_overrides.update(
                temperature=required("RESEARCHOS_TEMPERATURE"),
                top_p=required("RESEARCHOS_TOP_P"),
            )
        if role == "agent" and enable_agent_tool_calls:
            policy_overrides["provider_total_call_timeout_ms"] = int(
                required("RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS")
            )
        if role == "claim_extraction" and require_phase10_roles:
            policy_overrides["provider_total_call_timeout_ms"] = int(
                required("RESEARCHOS_PROVIDER_TOTAL_CALL_TIMEOUT_MS")
            )
        phase11_planning_total_timeout_enabled = role == "planning" and bool(
            values.get("RESEARCHOS_PHASE11_PLANNING_TOTAL_CALL_TIMEOUT_MS")
        )
        if phase11_planning_total_timeout_enabled:
            policy_overrides["provider_total_call_timeout_ms"] = int(
                required("RESEARCHOS_PHASE11_PLANNING_TOTAL_CALL_TIMEOUT_MS")
            )
        policy = default_deepseek_policy(
            model_id=model_id,
            thinking_mode=thinking_mode,
            reasoning_effort=required(
                f"RESEARCHOS_DEEPSEEK_{role.upper()}_REASONING_EFFORT"
            ),
            **policy_overrides,
        )
        models.append(
            RealModelSettings(
                role_id=role,
                provider_profile_id="deepseek_default",
                model_id=model_id,
                adapter_id=f"deepseek_{role}",
                adapter_version=adapter_version,
                base_endpoint=endpoint,
                prompt_schema_version="deepseek-prompt-v1",
                prompt_content_hash=deepseek_prompt_content_hash(
                    role,
                    enable_web_tools=(role == "agent" and enable_web_tools),
                    enable_retrieval_tools=(role == "agent" and enable_retrieval_tools),
                    web_search_max_results=(
                        web_search_max_results if role == "agent" else None
                    ),
                    require_explicit_web_search_capability=(
                        phase11_planning_total_timeout_enabled
                    ),
                ),
                response_contract_version=deepseek_response_contract(
                    role,
                    enable_web_tools=(role == "agent" and enable_web_tools),
                    enable_retrieval_tools=(role == "agent" and enable_retrieval_tools),
                ),
                policy=policy,
                credential_slot_id=credential_slot_id,
                phase11_planning_total_timeout_enabled=(
                    phase11_planning_total_timeout_enabled
                ),
            )
        )
    return RealIntegrationSettings(
        models=tuple(sorted(models, key=lambda item: item.role_id)),
        capabilities=tuple(item.pin() for item in capability_settings),
        capability_settings=capability_settings,
    )
