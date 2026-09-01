"""Strict, immutable, non-persistent REAL integration settings."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256
from researchos.domain.identity import stable_hash
from researchos.domain.real_composition import (
    ENDPOINT_CANONICALIZATION_VERSION,
    CapabilityCompositionPin,
    DeepSeekPricingUpperBoundProfile,
    DeepSeekReasoningEffort,
    DeepSeekRequestPolicySnapshot,
    DeepSeekThinkingMode,
    DeterministicGenerationMode,
    ModelBundleSnapshot,
    ModelCallPolicySnapshot,
    SamplingControlMode,
)

REAL_SETTINGS_SCHEMA_VERSION = 1
DEEPSEEK_PRICING_SAFETY_PROFILE_VERSION = "deepseek-pricing-safety-2026-09-v2"


_DEEPSEEK_PRICING_SAFETY_PROFILES = {
    "deepseek-v4-flash": {
        "context_window_tokens": 1_000_000,
        "provider_max_output_tokens": 384_000,
        "minimum_safe_input_rate": 10_000_000,
        "minimum_safe_output_rate": 20_000_000,
        "billing_currency": "USD",
    },
    "deepseek-v4-pro": {
        "context_window_tokens": 1_000_000,
        "provider_max_output_tokens": 384_000,
        "minimum_safe_input_rate": 10_000_000,
        "minimum_safe_output_rate": 20_000_000,
        "billing_currency": "USD",
    },
}


def deepseek_pricing_safety_profile(
    model_id: str,
) -> DeepSeekPricingUpperBoundProfile:
    """Return the adapter-owned conservative pricing/admission profile."""

    try:
        values = _DEEPSEEK_PRICING_SAFETY_PROFILES[model_id]
    except KeyError as exc:
        raise ValueError(
            f"unsupported DeepSeek pricing safety model: {model_id}"
        ) from exc
    return DeepSeekPricingUpperBoundProfile.build(
        model_id=model_id,
        profile_version=DEEPSEEK_PRICING_SAFETY_PROFILE_VERSION,
        **values,
    )


def canonicalize_base_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("base endpoint must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base endpoint cannot contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("base endpoint cannot contain query or fragment")
    if not parsed.hostname:
        raise ValueError("base endpoint requires host")
    scheme = parsed.scheme.lower()
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base endpoint port is invalid")
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    raw_path = parsed.path or "/"
    segments: list[str] = []
    for segment in raw_path.split("/"):
        decoded = unquote(segment)
        if decoded in {".", ".."}:
            raise ValueError("base endpoint path cannot contain dot segments")
        segments.append(quote(decoded, safe="-._~"))
    path = "/".join(segments)
    if not path.startswith("/"):
        path = f"/{path}"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def base_endpoint_hash(value: str) -> str:
    return hashlib.sha256(canonicalize_base_endpoint(value).encode()).hexdigest()


class RealModelSettings(ContractModel):
    role_id: SafeId
    provider_id: Literal["deepseek"] = "deepseek"
    provider_profile_id: SafeId
    model_id: SafeId
    adapter_id: SafeId
    adapter_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    base_endpoint: Annotated[str, StringConstraints(min_length=1, max_length=2_048)]
    prompt_schema_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    prompt_content_hash: Sha256
    response_contract_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    policy: ModelCallPolicySnapshot
    credential_slot_id: SafeId

    @field_validator("base_endpoint")
    @classmethod
    def endpoint_is_canonicalizable(cls, value: str) -> str:
        canonical = canonicalize_base_endpoint(value)
        if not canonical.startswith("https://"):
            raise ValueError("DeepSeek base endpoint must use HTTPS")
        return value

    @model_validator(mode="after")
    def policy_matches_model(self) -> RealModelSettings:
        if self.policy.priced_model_id != self.model_id:
            raise ValueError("pricing upper-bound policy model differs")
        if self.policy.pricing_safety_profile != deepseek_pricing_safety_profile(
            self.model_id
        ):
            raise ValueError("pricing safety profile differs from adapter authority")
        return self

    def bundle(self) -> ModelBundleSnapshot:
        return ModelBundleSnapshot.build(
            policy=self.policy,
            role_id=self.role_id,
            provider_id=self.provider_id,
            provider_profile_id=self.provider_profile_id,
            model_id=self.model_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            credential_slot_id=self.credential_slot_id,
            endpoint_canonicalization_version=ENDPOINT_CANONICALIZATION_VERSION,
            base_endpoint_hash=base_endpoint_hash(self.base_endpoint),
            prompt_schema_version=self.prompt_schema_version,
            prompt_content_hash=self.prompt_content_hash,
            response_contract_version=self.response_contract_version,
        )


class RealIntegrationSettings(ContractModel):
    schema_version: Literal[REAL_SETTINGS_SCHEMA_VERSION] = REAL_SETTINGS_SCHEMA_VERSION
    models: tuple[RealModelSettings, ...]
    capabilities: tuple[CapabilityCompositionPin, ...] = ()
    source_policy_id: SafeId | None = None
    source_policy_hash: Sha256 = Field(default_factory=lambda: stable_hash(None))

    @model_validator(mode="after")
    def settings_are_canonical(self) -> RealIntegrationSettings:
        roles = tuple(item.role_id for item in self.models)
        if roles != tuple(sorted(roles)) or len(roles) != len(set(roles)):
            raise ValueError("REAL model settings must have unique sorted roles")
        capabilities = tuple(item.capability_id for item in self.capabilities)
        if capabilities != tuple(sorted(capabilities)) or len(capabilities) != len(
            set(capabilities)
        ):
            raise ValueError("REAL capability settings must be unique and sorted")
        no_policy_hash = stable_hash(None)
        if self.source_policy_id is None:
            if self.source_policy_hash != no_policy_hash:
                raise ValueError(
                    "absent source policy must use the canonical null hash"
                )
        elif self.source_policy_hash == no_policy_hash:
            raise ValueError("configured source policy requires its effective hash")
        return self

    def model_for_role(self, role_id: str) -> RealModelSettings:
        matches = tuple(item for item in self.models if item.role_id == role_id)
        if len(matches) != 1:
            raise ValueError(f"REAL model role is not configured: {role_id}")
        return matches[0]


def default_deepseek_policy(
    *,
    model_id: str = "deepseek-v4-pro",
    thinking_mode: DeepSeekThinkingMode | str = DeepSeekThinkingMode.ENABLED,
    reasoning_effort: DeepSeekReasoningEffort | str = DeepSeekReasoningEffort.HIGH,
    **overrides: object,
) -> ModelCallPolicySnapshot:
    thinking_mode = DeepSeekThinkingMode(thinking_mode)
    reasoning_effort = DeepSeekReasoningEffort(reasoning_effort)
    sampling_mode = (
        SamplingControlMode.IGNORED_BY_PROVIDER
        if thinking_mode is DeepSeekThinkingMode.ENABLED
        else SamplingControlMode.EFFECTIVE
    )
    pricing_safety_profile = deepseek_pricing_safety_profile(model_id)
    values: dict[str, object] = {
        "provider_request_policy": DeepSeekRequestPolicySnapshot(
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            sampling_control_mode=sampling_mode,
        ),
        "max_input_tokens": 32_768,
        "max_output_tokens": 4_096,
        "max_request_bytes": 1_048_576,
        "max_response_bytes": 1_048_576,
        "temperature": None if thinking_mode is DeepSeekThinkingMode.ENABLED else "0",
        "top_p": None if thinking_mode is DeepSeekThinkingMode.ENABLED else "1",
        "seed": None,
        "deterministic_generation": DeterministicGenerationMode.UNSUPPORTED,
        "connect_timeout_ms": 10_000,
        "read_timeout_ms": 60_000,
        "write_timeout_ms": 10_000,
        "pool_timeout_ms": 10_000,
        "cost_currency": "USD",
        "max_cost_microunits_per_call": 11_000_000,
        "pricing_policy_id": f"deepseek_{model_id.replace('-', '_')}_upper_bound",
        "pricing_policy_version": "upper-bound-v1",
        "priced_model_id": model_id,
        "input_cost_upper_bound_microunits_per_million_tokens": 10_000_000,
        "output_cost_upper_bound_microunits_per_million_tokens": 20_000_000,
        "pricing_safety_profile": pricing_safety_profile,
    }
    values.update(overrides)
    return ModelCallPolicySnapshot.build(**values)
