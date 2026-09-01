"""Narrow Phase 9 provider and secret interfaces."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class ProviderDispatchDiagnostic(StrEnum):
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED_OUTCOME_UNKNOWN = "dispatched_outcome_unknown"
    RESPONSE_RECEIVED = "response_received"


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
