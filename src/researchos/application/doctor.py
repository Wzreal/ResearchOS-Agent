"""Zero-cost, read-only Phase 9 REAL configuration diagnostics."""

from __future__ import annotations

import json
from enum import StrEnum
from importlib.util import find_spec
from typing import Literal

from pydantic import Field

from researchos.application.errors import RunConfigurationError
from researchos.application.real_composition import RealCompositionManager
from researchos.configuration.observability import OtlpHttpSettings
from researchos.configuration.real_settings import RealIntegrationSettings
from researchos.domain.contracts import ContractModel, RunState, SafeId
from researchos.interfaces.providers import SecretSource


class DoctorCheckStatus(StrEnum):
    PASS = "pass"
    PARTIALLY_VERIFIED = "partially_verified"
    FAIL = "fail"


class DoctorCheck(ContractModel):
    check_id: SafeId
    status: DoctorCheckStatus
    code: SafeId


class DoctorReport(ContractModel):
    schema_version: Literal[1] = 1
    mode: Literal["real"] = "real"
    paid_calls: int = Field(default=0, ge=0)
    remote_writes: int = Field(default=0, ge=0)
    checks: tuple[DoctorCheck, ...]


class RealDoctor:
    def __init__(
        self,
        *,
        settings: RealIntegrationSettings,
        secrets: SecretSource,
        composition: RealCompositionManager | None = None,
        observability: OtlpHttpSettings | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        self._composition = composition
        self._observability = observability

    def run(
        self,
        *,
        state: RunState | None = None,
        probe_paid: bool = False,
        probe_writes: bool = False,
    ) -> DoctorReport:
        if probe_paid or probe_writes:
            raise RunConfigurationError(
                "paid/write doctor probes are not implemented in Phase 9B"
            )
        checks: list[DoctorCheck] = [
            DoctorCheck(
                check_id="configuration",
                status=DoctorCheckStatus.PASS,
                code="configuration_valid",
            )
        ]
        secret_ids = tuple(
            sorted(
                {item.credential_slot_id for item in self._settings.models}
                | {
                    item.credential_slot_id
                    for item in self._settings.capability_settings
                    if item.credential_slot_id is not None
                }
            )
        )
        secrets = tuple(self._secrets.get_secret(secret_id) for secret_id in secret_ids)
        secrets_valid = all(
            isinstance(value, str) and bool(value.strip()) for value in secrets
        )
        checks.append(
            DoctorCheck(
                check_id="secret_presence",
                status=(
                    DoctorCheckStatus.PASS if secrets_valid else DoctorCheckStatus.FAIL
                ),
                code=("secrets_present" if secrets_valid else "secret_missing"),
            )
        )
        required_modules = {"httpx"}
        if self._observability is not None:
            required_modules.add("opentelemetry.proto")
        if any(
            item.capability_id == "web_browser"
            for item in self._settings.capability_settings
        ):
            required_modules.add("trafilatura")
        if any(
            item.capability_id == "managed_retrieval"
            for item in self._settings.capability_settings
        ):
            required_modules.add("pymilvus")
        extras_valid = all(find_spec(name) is not None for name in required_modules)
        checks.append(
            DoctorCheck(
                check_id="optional_dependencies",
                status=(
                    DoctorCheckStatus.PASS if extras_valid else DoctorCheckStatus.FAIL
                ),
                code=(
                    "optional_dependencies_present"
                    if extras_valid
                    else "optional_dependency_missing"
                ),
            )
        )
        if self._observability is not None:
            value = (
                self._secrets.get_secret(self._observability.auth_credential_slot_id)
                if self._observability.auth_credential_slot_id is not None
                else "configured"
            )
            checks.append(
                DoctorCheck(
                    check_id="observability_secret",
                    status=(
                        DoctorCheckStatus.PASS if value else DoctorCheckStatus.FAIL
                    ),
                    code=(
                        "observability_secret_present"
                        if value
                        else "observability_secret_missing"
                    ),
                )
            )
        if state is not None and self._composition is not None:
            try:
                self._composition.validate_bound_state(state)
            except Exception:
                checks.append(
                    DoctorCheck(
                        check_id="composition",
                        status=DoctorCheckStatus.FAIL,
                        code="composition_invalid",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        check_id="composition",
                        status=DoctorCheckStatus.PASS,
                        code="composition_valid",
                    )
                )
        checks.append(
            DoctorCheck(
                check_id="provider_availability",
                status=DoctorCheckStatus.PARTIALLY_VERIFIED,
                code="paid_probe_not_run",
            )
        )
        report = DoctorReport(checks=tuple(checks))
        encoded = json.dumps(
            report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        if any(
            isinstance(value, str) and value and value in encoded for value in secrets
        ):
            raise ValueError("doctor report contains a credential canary")
        return report
