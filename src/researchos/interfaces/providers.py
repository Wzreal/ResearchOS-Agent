"""Narrow Phase 9 provider and secret interfaces."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from researchos.domain.real_composition import ProviderSuboperationReservation


class ProviderDispatchDiagnostic(StrEnum):
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED_OUTCOME_UNKNOWN = "dispatched_outcome_unknown"
    RESPONSE_RECEIVED = "response_received"


class ProviderAdmissionProfile(StrEnum):
    """Application-only provider admission behavior for an Agent binding."""

    LEGACY_TASK_LIMIT_V1 = "legacy_task_limit_v1"
    ACCUMULATED_REMAINING_V1 = "accumulated_remaining_v1"


class SecretSource(Protocol):
    def get_secret(self, secret_id: str) -> str | None: ...


class ProviderDispatchAuthorizer(Protocol):
    def authorize(
        self,
        *,
        run_id: str,
        role_id: str,
        composition_hash: str | None = None,
        trusted_runtime_replan: bool = False,
    ) -> None: ...


@runtime_checkable
class ProviderReservedAgent(Protocol):
    @property
    def provider_call_reservation(self) -> ProviderSuboperationReservation: ...


@runtime_checkable
class ProviderAdmissionProfiledAgent(Protocol):
    @property
    def provider_admission_profile(self) -> ProviderAdmissionProfile: ...


@runtime_checkable
class ProviderReservedTool(Protocol):
    @property
    def provider_call_reservation(self) -> ProviderSuboperationReservation: ...
