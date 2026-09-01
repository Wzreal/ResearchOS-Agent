"""Environment-backed secret source with no persistence behavior."""

from __future__ import annotations

import os
from collections.abc import Mapping

from researchos.configuration.real_settings import (
    RealIntegrationSettings,
    RealModelSettings,
    default_deepseek_policy,
)
from researchos.configuration.validation import (
    DEEPSEEK_RESPONSE_CONTRACTS,
    deepseek_prompt_content_hash,
)


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
    models: list[RealModelSettings] = []
    for role in ("agent", "planning", "verification"):
        model_id = required(f"RESEARCHOS_DEEPSEEK_{role.upper()}_MODEL")
        thinking_mode = required(
            f"RESEARCHOS_DEEPSEEK_{role.upper()}_THINKING_MODE"
        )
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
            "pricing_policy_version": required(
                "RESEARCHOS_PRICING_POLICY_VERSION"
            ),
            "input_cost_upper_bound_microunits_per_million_tokens": int(
                required("RESEARCHOS_INPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS")
            ),
            "output_cost_upper_bound_microunits_per_million_tokens": int(
                required("RESEARCHOS_OUTPUT_COST_UPPER_BOUND_PER_MILLION_TOKENS")
            ),
        }
        if thinking_mode == "disabled":
            policy_overrides.update(
                temperature=required("RESEARCHOS_TEMPERATURE"),
                top_p=required("RESEARCHOS_TOP_P"),
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
            prompt_content_hash=deepseek_prompt_content_hash(role),
            response_contract_version=DEEPSEEK_RESPONSE_CONTRACTS[role],
            policy=policy,
            credential_slot_id=credential_slot_id,
        )
        )
    return RealIntegrationSettings(models=tuple(models))
