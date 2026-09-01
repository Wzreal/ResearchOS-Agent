"""REAL composition creation, compatibility, and process-local binding."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from researchos.application.errors import (
    RealCompositionNotFound,
    RunConfigurationError,
)
from researchos.configuration.real_settings import (
    RealIntegrationSettings,
    RealModelSettings,
)
from researchos.domain.contracts import OperatingMode, RunConfig, RunState, RunStatus
from researchos.domain.real_composition import (
    ModelBundleSnapshot,
    RealCompositionEnvelope,
    RealCompositionSnapshot,
    SemanticCompositionPayload,
)
from researchos.interfaces.lifecycle import RealCompositionStore
from researchos.interfaces.providers import SecretSource

REQUIRED_PHASE9A_MODEL_ROLES = frozenset({"agent", "planning", "verification"})


class PristineRealRunProbe(Protocol):
    def is_pristine(self, state: RunState) -> bool: ...


class RejectMissingCompositionProbe:
    def is_pristine(self, state: RunState) -> bool:
        del state
        return False


@dataclass(frozen=True, slots=True)
class BoundRealModel:
    run_id: str
    run_config_hash: str
    composition_hash: str
    settings: RealModelSettings
    bundle: ModelBundleSnapshot
    credential: str = field(repr=False)


class RealCompositionManager:
    def __init__(
        self,
        *,
        settings: RealIntegrationSettings,
        secrets: SecretSource,
        store: RealCompositionStore,
        pristine_probe: PristineRealRunProbe | None = None,
    ) -> None:
        self._settings = RealIntegrationSettings.model_validate(
            settings.model_dump(mode="python")
        )
        self._secrets = secrets
        self._store = store
        self._pristine = pristine_probe or RejectMissingCompositionProbe()
        self._bindings: dict[str, str] = {}
        self._lock = RLock()

    def validate_create(self, config: RunConfig) -> None:
        if config.mode is not OperatingMode.REAL:
            return
        self._validate_config_compatibility(config)
        self._require_secrets()

    def bind_created(self, state: RunState) -> None:
        if state.config.mode is not OperatingMode.REAL:
            return
        snapshot = self.snapshot_for(state)
        envelope = self._store.create(snapshot)
        self._bind(envelope)

    def validate_resume(self, state: RunState) -> None:
        if state.config.mode is not OperatingMode.REAL:
            return
        expected = self.snapshot_for(state)
        try:
            envelope = self._store.load(state.run_id)
        except RealCompositionNotFound:
            if not (
                state.status is RunStatus.CREATED
                and state.revision == 0
                and self._pristine.is_pristine(state)
            ):
                raise
            envelope = self._store.create(expected)
        self._require_equal(envelope, expected, state)
        self._require_secrets()
        self._bind(envelope)

    def validate_bound_state(self, state: RunState) -> None:
        if state.config.mode is not OperatingMode.REAL:
            return
        expected = self.snapshot_for(state)
        envelope = self._store.load(state.run_id)
        self._require_equal(envelope, expected, state)

    def validate_dispatch(self, state: RunState, composition_hash: str) -> None:
        """Revalidate durable authority and the local binding before HTTP."""
        if state.config.mode is not OperatingMode.REAL:
            raise RunConfigurationError("REAL provider requires a REAL Run")
        expected = self.snapshot_for(state)
        envelope = self._store.load(state.run_id)
        self._require_equal(envelope, expected, state)
        with self._lock:
            bound_hash = self._bindings.get(state.run_id)
        if (
            bound_hash != expected.composition_hash
            or composition_hash != expected.composition_hash
        ):
            raise RunConfigurationError(
                "REAL adapter binding differs from durable composition"
            )
        self._require_secrets()

    def validate_transition(self, state: RunState, target: RunStatus) -> None:
        if state.config.mode is not OperatingMode.REAL:
            return
        if state.status is RunStatus.CREATED and target is RunStatus.PLANNING:
            with self._lock:
                bound_hash = self._bindings.get(state.run_id)
            expected = self.snapshot_for(state)
            if bound_hash != expected.composition_hash:
                raise RunConfigurationError(
                    "REAL Run adapters are not bound to its composition"
                )

    def snapshot_for(self, state: RunState) -> RealCompositionSnapshot:
        self._validate_config_compatibility(state.config)
        semantic = SemanticCompositionPayload(
            run_id=state.run_id,
            run_config_hash=state.config_hash,
            model_bundles=tuple(item.bundle() for item in self._settings.models),
            capabilities=self._settings.capabilities,
            source_policy_id=self._settings.source_policy_id,
            source_policy_hash=self._settings.source_policy_hash,
        )
        return RealCompositionSnapshot.build(semantic)

    def bound_model(self, run_id: str, role_id: str) -> BoundRealModel:
        envelope = self._store.load(run_id)
        with self._lock:
            if self._bindings.get(run_id) != envelope.snapshot.composition_hash:
                raise RunConfigurationError(
                    "REAL adapter dispatch requires process-local binding"
                )
        settings = self._settings.model_for_role(role_id)
        bundle = next(
            item
            for item in envelope.snapshot.semantic.model_bundles
            if item.payload.role_id == role_id
        )
        credential = self._valid_secret(settings.credential_slot_id)
        if credential is None:
            raise RunConfigurationError("required REAL provider credential is absent")
        return BoundRealModel(
            run_id=run_id,
            run_config_hash=envelope.snapshot.semantic.run_config_hash,
            composition_hash=envelope.snapshot.composition_hash,
            settings=settings,
            bundle=bundle,
            credential=credential,
        )

    def _bind(self, envelope: RealCompositionEnvelope) -> None:
        with self._lock:
            self._bindings[
                envelope.snapshot.semantic.run_id
            ] = envelope.snapshot.composition_hash

    def _validate_config_compatibility(self, config: RunConfig) -> None:
        roles = frozenset(item.role_id for item in self._settings.models)
        if roles != REQUIRED_PHASE9A_MODEL_ROLES:
            raise RunConfigurationError(
                "REAL composition requires planning, agent, and verification models"
            )
        if self._settings.source_policy_id != config.source_policy_id:
            raise RunConfigurationError("REAL source policy differs from Run config")
        configured = tuple(item.capability_id for item in self._settings.capabilities)
        if configured or config.allowed_capability_ids:
            raise RunConfigurationError(
                "REAL Agent capability schemas are deferred until Phase 9B"
            )
        if configured != tuple(sorted(config.allowed_capability_ids)):
            raise RunConfigurationError("REAL capabilities differ from Run config")
        for model in self._settings.models:
            if model.policy.cost_currency != config.budget_limits.cost_currency:
                raise RunConfigurationError(
                    "REAL model cost currency differs from Run budget"
                )
            if (
                model.policy.provider_call_reservation.cost_microunits
                > config.budget_limits.max_cost_microunits
                or model.policy.provider_call_reservation.total_tokens
                > config.budget_limits.max_tokens
            ):
                raise RunConfigurationError(
                    "REAL provider-call reservation exceeds Run budget limit"
                )

    def _require_secrets(self) -> None:
        missing = tuple(
            item.credential_slot_id
            for item in self._settings.models
            if self._valid_secret(item.credential_slot_id) is None
        )
        if missing:
            raise RunConfigurationError("required REAL provider credential is absent")

    def _valid_secret(self, credential_slot_id: str) -> str | None:
        value = self._secrets.get_secret(credential_slot_id)
        if value is None or not isinstance(value, str) or not value.strip():
            return None
        return value

    @staticmethod
    def _require_equal(
        envelope: RealCompositionEnvelope,
        expected: RealCompositionSnapshot,
        state: RunState,
    ) -> None:
        if envelope.snapshot.semantic.run_id != state.run_id:
            raise RunConfigurationError("REAL composition Run identity differs")
        if envelope.snapshot.semantic.run_config_hash != state.config_hash:
            raise RunConfigurationError("REAL composition Run config hash differs")
        if (
            envelope.snapshot.composition_hash != expected.composition_hash
            or envelope.snapshot.semantic != expected.semantic
            or envelope.snapshot.composition_id != expected.composition_id
        ):
            raise RunConfigurationError(
                "REAL composition differs from current settings"
            )
