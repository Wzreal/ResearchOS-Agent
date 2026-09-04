"""Phase 9A immutable REAL integration composition contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from researchos.domain.contracts import (
    ContractModel,
    SafeId,
    Sha256,
    _require_aware,
)
from researchos.domain.identity import stable_hash, stable_id

REAL_COMPOSITION_SCHEMA_VERSION = 1
MODEL_CALL_POLICY_VERSION = 4
MODEL_BUNDLE_VERSION = 1
SEMANTIC_COMPOSITION_VERSION = 1
ENDPOINT_CANONICALIZATION_VERSION = "provider-base-endpoint-v1"
MAX_REAL_COMPOSITION_BYTES = 1_048_576

CanonicalDecimal = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=32),
]


class DeterministicGenerationMode(StrEnum):
    UNSUPPORTED = "unsupported"
    PROVIDER_SEED = "provider_seed"


class DeepSeekThinkingMode(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


class DeepSeekReasoningEffort(StrEnum):
    LOW = "low"
    HIGH = "high"
    MAX = "max"


class SamplingControlMode(StrEnum):
    EFFECTIVE = "effective"
    IGNORED_BY_PROVIDER = "ignored_by_provider"


class DeepSeekRequestPolicySnapshot(ContractModel):
    """Versioned provider request semantics without prompt or secret data."""

    schema_version: Literal[1] = 1
    canonicalization_version: Literal["deepseek-request-policy-v1"] = (
        "deepseek-request-policy-v1"
    )
    thinking_mode: DeepSeekThinkingMode
    reasoning_effort: DeepSeekReasoningEffort
    sampling_control_mode: SamplingControlMode

    @model_validator(mode="after")
    def controls_match_thinking_mode(self) -> DeepSeekRequestPolicySnapshot:
        expected = (
            SamplingControlMode.IGNORED_BY_PROVIDER
            if self.thinking_mode is DeepSeekThinkingMode.ENABLED
            else SamplingControlMode.EFFECTIVE
        )
        if self.sampling_control_mode is not expected:
            raise ValueError("sampling control mode differs from thinking mode")
        return self


class DeepSeekPricingUpperBoundProfile(ContractModel):
    """Adapter-owned safety floor and provider-call token reservation."""

    schema_version: Literal[2] = 2
    canonicalization_version: Literal["deepseek-pricing-safety-v2"] = (
        "deepseek-pricing-safety-v2"
    )
    model_id: SafeId
    profile_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    context_window_tokens: int = Field(gt=0, le=10_000_000)
    provider_max_output_tokens: int = Field(gt=0, le=1_000_000)
    minimum_safe_input_rate: int = Field(gt=0)
    minimum_safe_output_rate: int = Field(gt=0)
    billing_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    profile_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> DeepSeekPricingUpperBoundProfile:
        if self.profile_hash != self.compute_hash():
            raise ValueError("DeepSeek pricing safety profile hash differs")
        return self

    def hash_preimage(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"profile_hash"})

    def compute_hash(self) -> str:
        return stable_hash(self.hash_preimage())

    @classmethod
    def build(cls, **values: object) -> DeepSeekPricingUpperBoundProfile:
        candidate = cls.model_construct(**values, profile_hash="0" * 64)
        payload = candidate.model_dump(mode="json", exclude={"profile_hash"})
        return cls.model_validate({**payload, "profile_hash": stable_hash(payload)})


class ProviderCallReservation(ContractModel):
    """Read-only pre-dispatch upper bound; it is not a budget ledger."""

    schema_version: Literal[1] = 1
    reservation_version: Literal["provider-call-reservation-v1"] = (
        "provider-call-reservation-v1"
    )
    input_tokens: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    total_tokens: int = Field(gt=0)
    cost_microunits: int = Field(gt=0)
    cost_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

    @model_validator(mode="after")
    def total_is_valid(self) -> ProviderCallReservation:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("provider-call token reservation total differs")
        return self


class ProviderSuboperationReservation(ContractModel):
    """Immutable upper bound used for local sub-operation admission only."""

    schema_version: Literal[1] = 1
    reservation_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    duration_milliseconds: int = Field(ge=0, le=3_600_000)
    tokens: int = Field(ge=0, le=10_000_000)
    cost_microunits: int = Field(ge=0)
    cost_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    tool_calls: int = Field(ge=0, le=1_000)
    pricing_policy_id: SafeId
    pricing_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    pricing_rules_hash: Sha256
    reservation_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> ProviderSuboperationReservation:
        if self.reservation_hash != self.compute_hash():
            raise ValueError("provider sub-operation reservation hash differs")
        return self

    def compute_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json", exclude={"reservation_hash"}))

    @classmethod
    def build(cls, **values: object) -> ProviderSuboperationReservation:
        candidate = cls.model_construct(**values, reservation_hash="0" * 64)
        payload = candidate.model_dump(mode="json", exclude={"reservation_hash"})
        return cls.model_validate({**payload, "reservation_hash": stable_hash(payload)})


def _validate_decimal(value: str, *, minimum: Decimal, maximum: Decimal) -> str:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("generation parameter must be canonical decimal") from exc
    if not parsed.is_finite() or not minimum <= parsed <= maximum:
        raise ValueError("generation parameter is outside supported range")
    canonical = format(parsed.normalize(), "f")
    if canonical == "-0":
        canonical = "0"
    if canonical != value:
        raise ValueError("generation parameter must use canonical decimal form")
    return value


def compute_pricing_rules_hash(
    *,
    pricing_policy_id: str,
    pricing_policy_version: str,
    priced_model_id: str,
    pricing_safety_profile_hash: str,
    cost_currency: str,
    input_cost_upper_bound_microunits_per_million_tokens: int,
    output_cost_upper_bound_microunits_per_million_tokens: int,
) -> str:
    return stable_hash(
        {
            "pricing_policy_id": pricing_policy_id,
            "pricing_policy_version": pricing_policy_version,
            "priced_model_id": priced_model_id,
            "pricing_safety_profile_hash": pricing_safety_profile_hash,
            "cost_currency": cost_currency,
            "input_cost_upper_bound_microunits_per_million_tokens": (
                input_cost_upper_bound_microunits_per_million_tokens
            ),
            "output_cost_upper_bound_microunits_per_million_tokens": (
                output_cost_upper_bound_microunits_per_million_tokens
            ),
        }
    )


class ModelCallPolicySnapshot(ContractModel):
    """Versioned effective request, termination, and cost policy."""

    schema_version: Literal[MODEL_CALL_POLICY_VERSION] = MODEL_CALL_POLICY_VERSION
    canonicalization_version: Literal["model-call-policy-v4"] = (
        "model-call-policy-v4"
    )
    provider_request_policy: DeepSeekRequestPolicySnapshot
    pricing_safety_profile: DeepSeekPricingUpperBoundProfile
    provider_call_reservation: ProviderCallReservation
    max_input_tokens: int = Field(gt=0, le=10_000_000)
    max_output_tokens: int = Field(gt=0, le=1_000_000)
    max_request_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    max_response_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    temperature: CanonicalDecimal | None
    top_p: CanonicalDecimal | None
    seed: int | None = None
    deterministic_generation: DeterministicGenerationMode
    connect_timeout_ms: int = Field(gt=0, le=3_600_000)
    read_timeout_ms: int = Field(gt=0, le=3_600_000)
    write_timeout_ms: int = Field(gt=0, le=3_600_000)
    pool_timeout_ms: int = Field(gt=0, le=3_600_000)
    provider_total_call_timeout_ms: int | None = Field(default=None, gt=0, le=3_600_000)
    cost_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    max_cost_microunits_per_call: int = Field(gt=0)
    pricing_policy_id: SafeId
    pricing_policy_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    priced_model_id: SafeId
    pricing_rules_hash: Sha256
    input_cost_upper_bound_microunits_per_million_tokens: int = Field(gt=0)
    output_cost_upper_bound_microunits_per_million_tokens: int = Field(gt=0)
    model_call_policy_hash: Sha256

    @field_validator("temperature")
    @classmethod
    def temperature_is_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_decimal(value, minimum=Decimal("0"), maximum=Decimal("2"))

    @field_validator("top_p")
    @classmethod
    def top_p_is_canonical(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_decimal(value, minimum=Decimal("0"), maximum=Decimal("1"))

    @model_validator(mode="after")
    def identity_is_valid(self) -> ModelCallPolicySnapshot:
        sampling_effective = (
            self.provider_request_policy.sampling_control_mode
            is SamplingControlMode.EFFECTIVE
        )
        if (
            sampling_effective
            and (self.temperature is None or self.top_p is None)
        ) or (
            not sampling_effective
            and (self.temperature is not None or self.top_p is not None)
        ):
            raise ValueError(
                "effective sampling requires values; ignored sampling requires nulls"
            )
        if self.deterministic_generation is DeterministicGenerationMode.UNSUPPORTED:
            if self.seed is not None:
                raise ValueError("unsupported deterministic mode cannot contain seed")
        elif self.seed is None:
            raise ValueError("provider seed mode requires seed")
        expected_pricing_hash = compute_pricing_rules_hash(
            pricing_policy_id=self.pricing_policy_id,
            pricing_policy_version=self.pricing_policy_version,
            priced_model_id=self.priced_model_id,
            pricing_safety_profile_hash=self.pricing_safety_profile.profile_hash,
            cost_currency=self.cost_currency,
            input_cost_upper_bound_microunits_per_million_tokens=(
                self.input_cost_upper_bound_microunits_per_million_tokens
            ),
            output_cost_upper_bound_microunits_per_million_tokens=(
                self.output_cost_upper_bound_microunits_per_million_tokens
            ),
        )
        if self.pricing_rules_hash != expected_pricing_hash:
            raise ValueError("pricing rules hash differs")
        if self.pricing_safety_profile.model_id != self.priced_model_id:
            raise ValueError("pricing safety profile model differs")
        if self.cost_currency != self.pricing_safety_profile.billing_currency:
            raise ValueError("policy currency differs from pricing safety profile")
        if (
            self.input_cost_upper_bound_microunits_per_million_tokens
            < self.pricing_safety_profile.minimum_safe_input_rate
        ):
            raise ValueError("configured input rate is below adapter safety floor")
        if (
            self.output_cost_upper_bound_microunits_per_million_tokens
            < self.pricing_safety_profile.minimum_safe_output_rate
        ):
            raise ValueError("configured output rate is below adapter safety floor")
        if self.max_input_tokens > self.pricing_safety_profile.context_window_tokens:
            raise ValueError("post-response input cap exceeds provider context window")
        if (
            self.max_output_tokens
            > self.pricing_safety_profile.provider_max_output_tokens
        ):
            raise ValueError("output cap exceeds provider model limit")
        reserved_cost = (
            self.pricing_safety_profile.context_window_tokens
            * self.input_cost_upper_bound_microunits_per_million_tokens
            + self.max_output_tokens
            * self.output_cost_upper_bound_microunits_per_million_tokens
            + 999_999
        ) // 1_000_000
        expected_reservation = ProviderCallReservation(
            input_tokens=self.pricing_safety_profile.context_window_tokens,
            output_tokens=self.max_output_tokens,
            total_tokens=(
                self.pricing_safety_profile.context_window_tokens
                + self.max_output_tokens
            ),
            cost_microunits=reserved_cost,
            cost_currency=self.cost_currency,
        )
        if self.provider_call_reservation != expected_reservation:
            raise ValueError("provider-call reservation differs from policy")
        if reserved_cost > self.max_cost_microunits_per_call:
            raise ValueError("per-call cost ceiling is below pricing upper bound")
        if self.model_call_policy_hash != self.compute_hash():
            raise ValueError("model call policy hash differs")
        return self

    def hash_preimage(self) -> dict[str, object]:
        excluded = {"model_call_policy_hash"}
        if self.provider_total_call_timeout_ms is None:
            # Preserve the Phase 9A direct-profile hash preimage.
            excluded.add("provider_total_call_timeout_ms")
        return self.model_dump(mode="json", exclude=excluded)

    def compute_hash(self) -> str:
        return stable_hash(self.hash_preimage())

    @classmethod
    def build(cls, **values: object) -> ModelCallPolicySnapshot:
        if "deterministic_generation" in values:
            values["deterministic_generation"] = DeterministicGenerationMode(
                values["deterministic_generation"]
            )
        if "provider_request_policy" in values and isinstance(
            values["provider_request_policy"], dict
        ):
            values["provider_request_policy"] = (
                DeepSeekRequestPolicySnapshot.model_validate(
                    values["provider_request_policy"]
                )
            )
        if "pricing_safety_profile" in values and isinstance(
            values["pricing_safety_profile"], dict
        ):
            values["pricing_safety_profile"] = (
                DeepSeekPricingUpperBoundProfile.model_validate(
                    values["pricing_safety_profile"]
                )
            )
        profile = values["pricing_safety_profile"]
        assert isinstance(profile, DeepSeekPricingUpperBoundProfile)
        if "provider_call_reservation" not in values:
            output_tokens = int(values["max_output_tokens"])
            input_rate = int(
                values["input_cost_upper_bound_microunits_per_million_tokens"]
            )
            output_rate = int(
                values["output_cost_upper_bound_microunits_per_million_tokens"]
            )
            reserved_cost = (
                profile.context_window_tokens * input_rate
                + output_tokens * output_rate
                + 999_999
            ) // 1_000_000
            values["provider_call_reservation"] = ProviderCallReservation(
                input_tokens=profile.context_window_tokens,
                output_tokens=output_tokens,
                total_tokens=profile.context_window_tokens + output_tokens,
                cost_microunits=reserved_cost,
                cost_currency=str(values["cost_currency"]),
            )
        if "pricing_rules_hash" not in values:
            values["pricing_rules_hash"] = compute_pricing_rules_hash(
                pricing_policy_id=str(values["pricing_policy_id"]),
                pricing_policy_version=str(values["pricing_policy_version"]),
                priced_model_id=str(values["priced_model_id"]),
                pricing_safety_profile_hash=profile.profile_hash,
                cost_currency=str(values["cost_currency"]),
                input_cost_upper_bound_microunits_per_million_tokens=int(
                    values[
                        "input_cost_upper_bound_microunits_per_million_tokens"
                    ]
                ),
                output_cost_upper_bound_microunits_per_million_tokens=int(
                    values[
                        "output_cost_upper_bound_microunits_per_million_tokens"
                    ]
                ),
            )
        candidate = cls.model_construct(
            **values,
            model_call_policy_hash="0" * 64,
        )
        payload = candidate.hash_preimage()
        return cls.model_validate(
            {**payload, "model_call_policy_hash": stable_hash(payload)}
        )


class ModelBundlePayload(ContractModel):
    schema_version: Literal[MODEL_BUNDLE_VERSION] = MODEL_BUNDLE_VERSION
    canonicalization_version: Literal["model-bundle-v1"] = "model-bundle-v1"
    role_id: SafeId
    provider_id: SafeId
    provider_profile_id: SafeId
    model_id: SafeId
    adapter_id: SafeId
    adapter_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    credential_slot_id: SafeId
    endpoint_canonicalization_version: Literal[
        "provider-base-endpoint-v1"
    ] = ENDPOINT_CANONICALIZATION_VERSION
    base_endpoint_hash: Sha256
    prompt_schema_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    prompt_content_hash: Sha256
    response_contract_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    model_call_policy_hash: Sha256


class ModelBundleSnapshot(ContractModel):
    policy: ModelCallPolicySnapshot
    payload: ModelBundlePayload
    model_bundle_hash: Sha256

    @model_validator(mode="after")
    def identity_is_valid(self) -> ModelBundleSnapshot:
        if self.payload.model_call_policy_hash != self.policy.model_call_policy_hash:
            raise ValueError("model bundle policy hash differs")
        if self.model_bundle_hash != stable_hash(self.payload.model_dump(mode="json")):
            raise ValueError("model bundle hash differs")
        return self

    @classmethod
    def build(
        cls, *, policy: ModelCallPolicySnapshot, **payload_values: object
    ) -> ModelBundleSnapshot:
        payload = ModelBundlePayload(
            **payload_values,
            model_call_policy_hash=policy.model_call_policy_hash,
        )
        return cls(
            policy=policy,
            payload=payload,
            model_bundle_hash=stable_hash(payload.model_dump(mode="json")),
        )


class CapabilityCompositionPin(ContractModel):
    capability_id: SafeId
    adapter_id: SafeId
    adapter_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    operation_version: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    policy_schema_version: Annotated[
        str, StringConstraints(min_length=1, max_length=80)
    ]
    policy_hash: Sha256
    descriptor_hash: Sha256
    provider_reservation_hash: Sha256
    tool_id: SafeId | None = None


class SemanticCompositionPayload(ContractModel):
    schema_version: Literal[SEMANTIC_COMPOSITION_VERSION] = (
        SEMANTIC_COMPOSITION_VERSION
    )
    run_id: SafeId
    run_config_hash: Sha256
    enabled_modes: tuple[Literal["real"], ...] = ("real",)
    model_bundles: tuple[ModelBundleSnapshot, ...]
    capabilities: tuple[CapabilityCompositionPin, ...] = ()
    source_policy_id: SafeId | None = None
    source_policy_hash: Sha256

    @model_validator(mode="after")
    def identities_are_unique_and_sorted(self) -> SemanticCompositionPayload:
        roles = tuple(bundle.payload.role_id for bundle in self.model_bundles)
        if len(roles) != len(set(roles)):
            raise ValueError("duplicate model bundle role")
        if roles != tuple(sorted(roles)):
            raise ValueError("model bundles must use canonical role order")
        capability_ids = tuple(item.capability_id for item in self.capabilities)
        if len(capability_ids) != len(set(capability_ids)):
            raise ValueError("duplicate capability composition pin")
        if capability_ids != tuple(sorted(capability_ids)):
            raise ValueError("capability pins must use canonical order")
        return self


class RealCompositionSnapshot(ContractModel):
    schema_version: Literal[REAL_COMPOSITION_SCHEMA_VERSION] = (
        REAL_COMPOSITION_SCHEMA_VERSION
    )
    snapshot_version: Literal[1] = 1
    semantic: SemanticCompositionPayload
    composition_hash: Sha256
    composition_id: SafeId

    @model_validator(mode="after")
    def identity_is_valid(self) -> RealCompositionSnapshot:
        expected_hash = stable_hash(self.semantic.model_dump(mode="json"))
        if self.composition_hash != expected_hash:
            raise ValueError("REAL composition semantic hash differs")
        expected_id = stable_id(
            "realcomp",
            [
                self.semantic.run_id,
                self.semantic.run_config_hash,
                expected_hash,
            ],
        )
        if self.composition_id != expected_id:
            raise ValueError("REAL composition ID differs")
        return self

    @classmethod
    def build(cls, semantic: SemanticCompositionPayload) -> RealCompositionSnapshot:
        composition_hash = stable_hash(semantic.model_dump(mode="json"))
        return cls(
            semantic=semantic,
            composition_hash=composition_hash,
            composition_id=stable_id(
                "realcomp",
                [semantic.run_id, semantic.run_config_hash, composition_hash],
            ),
        )


class RealCompositionEnvelope(ContractModel):
    schema_version: Literal[REAL_COMPOSITION_SCHEMA_VERSION] = (
        REAL_COMPOSITION_SCHEMA_VERSION
    )
    snapshot: RealCompositionSnapshot
    recorded_at: datetime
    artifact_content_hash: Sha256

    _aware_recorded_at = field_validator("recorded_at")(_require_aware)

    @model_validator(mode="after")
    def artifact_hash_is_valid(self) -> RealCompositionEnvelope:
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"artifact_content_hash"})
        )
        if self.artifact_content_hash != expected:
            raise ValueError("REAL composition artifact content hash differs")
        return self

    @classmethod
    def build(
        cls, snapshot: RealCompositionSnapshot, *, recorded_at: datetime
    ) -> RealCompositionEnvelope:
        candidate = cls.model_construct(
            snapshot=snapshot,
            recorded_at=recorded_at,
            artifact_content_hash="0" * 64,
        )
        payload = candidate.model_dump(mode="json", exclude={"artifact_content_hash"})
        return cls.model_validate(
            {**payload, "artifact_content_hash": stable_hash(payload)}
        )
