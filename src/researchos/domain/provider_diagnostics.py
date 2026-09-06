"""Allowlisted, non-content-bearing provider and tool diagnostics."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from researchos.domain.contracts import ContractModel, SafeId, Sha256


class ResourceDiagnosticsV1(ContractModel):
    duration_milliseconds: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    cost_microunits: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class TavilyReservationDiagnosticsV1(ContractModel):
    schema_version: Literal[1]
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


class ProviderDiagnosticsV1(ContractModel):
    schema_version: Literal["provider_diagnostics_v1"]
    role_id: SafeId
    operation_id: SafeId | None = None
    dispatch_classification: Literal[
        "not_dispatched", "response_received", "dispatched_outcome_unknown"
    ]
    http_response_received: bool
    provider_finish_reason: SafeId | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    canonical_request_bytes: int = Field(ge=0)
    error_code: SafeId | None = None
    retryable: bool
    duration_milliseconds: int = Field(ge=0)


class ToolDiagnosticsV1(ContractModel):
    schema_version: Literal["tool_diagnostics_v1"]
    adapter_id: SafeId
    dispatch_classification: Literal[
        "not_dispatched", "response_received", "dispatched_outcome_unknown"
    ]
    http_dispatch_attempted: bool
    http_response_received: bool
    error_code: SafeId | None = None
    retryable: bool
    duration_milliseconds: int = Field(ge=0)
    result_count: int | None = Field(default=None, ge=0)
    provider_reservation: TavilyReservationDiagnosticsV1
    settlement: ResourceDiagnosticsV1 | None = None


def validate_provider_diagnostics(
    value: dict[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return ProviderDiagnosticsV1.model_validate(value).model_dump(mode="json")


def validate_tool_diagnostics(
    value: dict[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    return ToolDiagnosticsV1.model_validate(value).model_dump(mode="json")
