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
    ProviderSuboperationReservation,
    SamplingControlMode,
)
from researchos.domain.retrieval import (
    ZILLIZ_BM25_ADAPTER_VERSION,
    ZillizBm25CollectionSchema,
    ZillizBm25RetrievalPolicySnapshot,
    canonicalize_zilliz_free_endpoint,
)
from researchos.domain.runtime import IdempotencyMode
from researchos.domain.tools import (
    AdapterMode,
    ToolDescriptor,
    ToolSideEffect,
)
from researchos.domain.web import (
    BROWSER_ADAPTER_VERSION,
    TAVILY_ADAPTER_VERSION,
    TAVILY_STANDARD_ENDPOINT,
    BrowserAddressPolicySnapshot,
    HttpBrowserPolicySnapshot,
    TavilyPricingSafetyProfile,
    TavilyProviderRequestPolicyV1,
    TavilyResearchOSPolicyV1,
    TavilySearchPolicySnapshot,
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
        is_web_agent = self.response_contract_version in {
            "agent-tool-decision-v2",
            "agent-tool-decision-v3",
        }
        if is_web_agent and self.policy.provider_total_call_timeout_ms is None:
            raise ValueError("web Agent requires a total provider-call timeout")
        if not is_web_agent and self.policy.provider_total_call_timeout_ms is not None:
            raise ValueError(
                "only the web Agent profile may set a total provider timeout"
            )
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

    def suboperation_reservation(self) -> ProviderSuboperationReservation:
        reservation = self.policy.provider_call_reservation
        if self.policy.provider_total_call_timeout_ms is None:
            raise ValueError(
                "provider suboperation timeout is unavailable for this profile"
            )
        return ProviderSuboperationReservation.build(
            reservation_version="deepseek-provider-suboperation-v1",
            duration_milliseconds=self.policy.provider_total_call_timeout_ms,
            tokens=reservation.total_tokens,
            cost_microunits=reservation.cost_microunits,
            cost_currency=reservation.cost_currency,
            tool_calls=0,
            pricing_policy_id=self.policy.pricing_policy_id,
            pricing_policy_version=self.policy.pricing_policy_version,
            pricing_rules_hash=self.policy.pricing_rules_hash,
        )


class RealCapabilitySettings(ContractModel):
    capability_id: SafeId
    tool_id: SafeId
    adapter_id: SafeId
    adapter_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    input_type: SafeId
    output_type: SafeId
    side_effect: ToolSideEffect
    idempotency: IdempotencyMode
    policy_schema_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    policy_hash: Sha256
    provider_reservation: ProviderSuboperationReservation
    tavily_policy: TavilySearchPolicySnapshot | None = None
    browser_policy: HttpBrowserPolicySnapshot | None = None
    retrieval_policy: ZillizBm25RetrievalPolicySnapshot | None = None
    credential_slot_id: SafeId | None = None

    @model_validator(mode="after")
    def policy_is_exact(self) -> RealCapabilitySettings:
        selected = tuple(
            item
            for item in (self.tavily_policy, self.browser_policy, self.retrieval_policy)
            if item is not None
        )
        if len(selected) != 1:
            raise ValueError("REAL capability requires exactly one policy")
        policy = selected[0]
        if policy.policy_hash != self.policy_hash:
            raise ValueError("REAL capability policy hash differs")
        policy_reservation = (
            policy.researchos.provider_reservation
            if isinstance(policy, TavilySearchPolicySnapshot)
            else policy.provider_reservation
        )
        if policy_reservation != self.provider_reservation:
            raise ValueError("REAL capability reservation differs")
        if self.tavily_policy is not None:
            if (
                self.capability_id,
                self.tool_id,
                self.adapter_id,
                self.adapter_version,
                self.input_type,
                self.output_type,
            ) != (
                "web_search",
                "tavily_search",
                "tavily_http",
                TAVILY_ADAPTER_VERSION,
                "search",
                "search_result_v2",
            ):
                raise ValueError("Tavily capability descriptor differs")
            if self.credential_slot_id is None:
                raise ValueError("Tavily capability requires credential slot")
            if (
                self.credential_slot_id
                != self.tavily_policy.researchos.credential_slot_id
            ):
                raise ValueError("Tavily credential-slot identity differs")
        elif self.browser_policy is not None:
            if (
                self.capability_id,
                self.tool_id,
                self.adapter_id,
                self.adapter_version,
                self.input_type,
                self.output_type,
            ) != (
                "web_browser",
                "http_browser",
                "bounded_http_browser",
                BROWSER_ADAPTER_VERSION,
                "browser",
                "browser_result",
            ):
                raise ValueError("Browser capability descriptor differs")
            if self.credential_slot_id is not None:
                raise ValueError("Browser capability cannot contain credential slot")
        else:
            assert self.retrieval_policy is not None
            if (
                self.capability_id,
                self.tool_id,
                self.adapter_id,
                self.adapter_version,
                self.input_type,
                self.output_type,
            ) != (
                "managed_retrieval",
                "zilliz_bm25_retrieval",
                "zilliz_milvus_bm25",
                ZILLIZ_BM25_ADAPTER_VERSION,
                "local_retrieval",
                "local_retrieval_result",
            ):
                raise ValueError("Zilliz retrieval capability descriptor differs")
            if self.credential_slot_id != self.retrieval_policy.credential_slot_id:
                raise ValueError("Zilliz retrieval credential-slot identity differs")
        return self

    @property
    def operation_version(self) -> str:
        return f"1+p{self.policy_hash}"

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            tool_id=self.tool_id,
            capability_id=self.capability_id,
            adapter_id=self.adapter_id,
            mode=AdapterMode.REAL,
            operation_version=self.operation_version,
            input_type=self.input_type,
            output_type=self.output_type,
            side_effect=self.side_effect,
            idempotency=self.idempotency,
        )

    def pin(self) -> CapabilityCompositionPin:
        descriptor = self.descriptor()
        return CapabilityCompositionPin(
            capability_id=self.capability_id,
            tool_id=self.tool_id,
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            operation_version=self.operation_version,
            policy_schema_version=self.policy_schema_version,
            policy_hash=self.policy_hash,
            descriptor_hash=stable_hash(descriptor.model_dump(mode="json")),
            provider_reservation_hash=self.provider_reservation.reservation_hash,
        )


class RealIntegrationSettings(ContractModel):
    schema_version: Literal[REAL_SETTINGS_SCHEMA_VERSION] = REAL_SETTINGS_SCHEMA_VERSION
    models: tuple[RealModelSettings, ...]
    capabilities: tuple[CapabilityCompositionPin, ...] = ()
    capability_settings: tuple[RealCapabilitySettings, ...] = ()
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
        configured = tuple(item.capability_id for item in self.capability_settings)
        if configured != tuple(sorted(configured)) or len(configured) != len(
            set(configured)
        ):
            raise ValueError("REAL capability policies must be unique and sorted")
        if tuple(item.pin() for item in self.capability_settings) != self.capabilities:
            raise ValueError("REAL capability pins differ from capability policies")
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

    def capability_for_id(self, capability_id: str) -> RealCapabilitySettings:
        matches = tuple(
            item
            for item in self.capability_settings
            if item.capability_id == capability_id
        )
        if len(matches) != 1:
            raise ValueError(f"REAL capability is not configured: {capability_id}")
        return matches[0]


def default_tavily_capability(
    *,
    max_results: int = 5,
    microunits_per_credit_upper_bound: int = 100_000,
    credential_slot_id: str = "researchos_tavily_api_key",
) -> RealCapabilitySettings:
    pricing = TavilyPricingSafetyProfile.standard()
    rules_hash = stable_hash(
        {
            "pricing_policy_id": "tavily_credit_upper_bound",
            "pricing_policy_version": "upper-bound-v1",
            "pricing_profile_hash": pricing.profile_hash,
            "microunits_per_credit_upper_bound": (microunits_per_credit_upper_bound),
        }
    )
    reservation = ProviderSuboperationReservation.build(
        reservation_version="tavily-provider-suboperation-v1",
        duration_milliseconds=60_000,
        tokens=0,
        cost_microunits=(
            pricing.maximum_credits_per_call * microunits_per_credit_upper_bound
        ),
        cost_currency=pricing.billing_currency,
        tool_calls=1,
        pricing_policy_id="tavily_credit_upper_bound",
        pricing_policy_version="upper-bound-v1",
        pricing_rules_hash=rules_hash,
    )
    internal = TavilyResearchOSPolicyV1(
        canonical_endpoint_hash=base_endpoint_hash(TAVILY_STANDARD_ENDPOINT),
        credential_slot_id=credential_slot_id,
        pricing_profile=pricing,
        microunits_per_credit_upper_bound=microunits_per_credit_upper_bound,
        pricing_rules_hash=rules_hash,
        provider_reservation=reservation,
    )
    request = TavilyProviderRequestPolicyV1(max_results=max_results)
    candidate = TavilySearchPolicySnapshot.model_construct(
        provider_request=request,
        researchos=internal,
        policy_hash="0" * 64,
    )
    policy = TavilySearchPolicySnapshot(
        provider_request=request,
        researchos=internal,
        policy_hash=candidate.compute_hash(),
    )
    return RealCapabilitySettings(
        capability_id="web_search",
        tool_id="tavily_search",
        adapter_id="tavily_http",
        adapter_version=TAVILY_ADAPTER_VERSION,
        input_type="search",
        output_type="search_result_v2",
        side_effect=ToolSideEffect.EXTERNAL,
        idempotency=IdempotencyMode.IDEMPOTENT,
        policy_schema_version="tavily-search-policy-v1",
        policy_hash=policy.policy_hash,
        provider_reservation=reservation,
        tavily_policy=policy,
        credential_slot_id=credential_slot_id,
    )


def default_browser_capability(
    *, total_invocation_timeout_ms: int = 250_000
) -> RealCapabilitySettings:
    rules_hash = stable_hash({"policy": "unmetered-http-browser-v1"})
    reservation = ProviderSuboperationReservation.build(
        reservation_version="browser-provider-suboperation-v1",
        duration_milliseconds=total_invocation_timeout_ms,
        tokens=0,
        cost_microunits=0,
        cost_currency="USD",
        tool_calls=1,
        pricing_policy_id="unmetered_http_browser",
        pricing_policy_version="v1",
        pricing_rules_hash=rules_hash,
    )

    candidate = HttpBrowserPolicySnapshot.model_construct(
        address_policy=BrowserAddressPolicySnapshot.standard(),
        provider_reservation=reservation,
        total_invocation_timeout_ms=total_invocation_timeout_ms,
        policy_hash="0" * 64,
    )
    policy = HttpBrowserPolicySnapshot(
        address_policy=BrowserAddressPolicySnapshot.standard(),
        provider_reservation=reservation,
        total_invocation_timeout_ms=total_invocation_timeout_ms,
        policy_hash=candidate.compute_hash(),
    )
    return RealCapabilitySettings(
        capability_id="web_browser",
        tool_id="http_browser",
        adapter_id="bounded_http_browser",
        adapter_version=BROWSER_ADAPTER_VERSION,
        input_type="browser",
        output_type="browser_result",
        side_effect=ToolSideEffect.NONE,
        idempotency=IdempotencyMode.IDEMPOTENT,
        policy_schema_version="bounded-http-browser-policy-v1",
        policy_hash=policy.policy_hash,
        provider_reservation=reservation,
        browser_policy=policy,
    )


def default_managed_retrieval_capability(
    *,
    endpoint: str,
    collection_id: str,
    credential_slot_id: str = "researchos_zilliz_token",
    max_results: int = 20,
    total_timeout_ms: int = 60_000,
) -> RealCapabilitySettings:
    endpoint = canonicalize_zilliz_free_endpoint(endpoint)
    rules_hash = stable_hash({"policy": "zilliz-cloud-free-zero-cost-v1"})
    reservation = ProviderSuboperationReservation.build(
        reservation_version="zilliz-free-provider-suboperation-v1",
        duration_milliseconds=total_timeout_ms,
        tokens=0,
        cost_microunits=0,
        cost_currency="USD",
        tool_calls=1,
        pricing_policy_id="zilliz_cloud_free_zero_cost",
        pricing_policy_version="v1",
        pricing_rules_hash=rules_hash,
    )
    values = {
        "endpoint": endpoint,
        "canonical_endpoint_hash": hashlib.sha256(endpoint.encode()).hexdigest(),
        "collection_id": collection_id,
        "collection_schema": ZillizBm25CollectionSchema.standard(),
        "max_results": max_results,
        "total_timeout_ms": total_timeout_ms,
        "credential_slot_id": credential_slot_id,
        "provider_reservation": reservation,
    }
    candidate = ZillizBm25RetrievalPolicySnapshot.model_construct(
        **values, policy_hash="0" * 64
    )
    policy = ZillizBm25RetrievalPolicySnapshot(
        **values, policy_hash=candidate.compute_hash()
    )
    return RealCapabilitySettings(
        capability_id="managed_retrieval",
        tool_id="zilliz_bm25_retrieval",
        adapter_id="zilliz_milvus_bm25",
        adapter_version=ZILLIZ_BM25_ADAPTER_VERSION,
        input_type="local_retrieval",
        output_type="local_retrieval_result",
        side_effect=ToolSideEffect.EXTERNAL,
        idempotency=IdempotencyMode.IDEMPOTENT,
        policy_schema_version="zilliz-bm25-retrieval-policy-v1",
        policy_hash=policy.policy_hash,
        provider_reservation=reservation,
        retrieval_policy=policy,
        credential_slot_id=credential_slot_id,
    )


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
