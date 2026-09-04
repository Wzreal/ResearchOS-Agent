from __future__ import annotations

from datetime import UTC, datetime, timedelta

from researchos.configuration.real_settings import (
    RealCapabilitySettings,
    RealIntegrationSettings,
    RealModelSettings,
    default_deepseek_policy,
)
from researchos.configuration.validation import (
    deepseek_prompt_content_hash,
    deepseek_response_contract,
)
from researchos.domain.contracts import (
    BudgetLimits,
    RunConfig,
    RunState,
    RunStatus,
    model_sha256,
)
from researchos.domain.real_composition import (
    RealCompositionSnapshot,
    SemanticCompositionPayload,
)


class FrozenClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **values: int) -> None:
        self.current += timedelta(**values)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self, prefix: str) -> str:
        self.value += 1
        return f"{prefix}_{self.value}"


def make_settings(
    *,
    policy_overrides: dict[str, object] | None = None,
    role_overrides: dict[str, dict[str, object]] | None = None,
    capability_settings: tuple[RealCapabilitySettings, ...] = (),
) -> RealIntegrationSettings:
    role_overrides = role_overrides or {}
    capability_settings = tuple(
        sorted(capability_settings, key=lambda item: item.capability_id)
    )
    enable_web_tools = bool(capability_settings)
    models = []
    for role in ("agent", "planning", "verification"):
        model_id = (
            "deepseek-v4-flash" if role == "agent" else "deepseek-v4-pro"
        )
        model_id = str(role_overrides.get(role, {}).get("model_id", model_id))
        effective_policy_overrides = dict(policy_overrides or {})
        if role == "agent" and enable_web_tools:
            effective_policy_overrides.setdefault(
                "provider_total_call_timeout_ms", 90_000
            )
        else:
            effective_policy_overrides.pop("provider_total_call_timeout_ms", None)
        policy = default_deepseek_policy(
            model_id=model_id, **effective_policy_overrides
        )
        values: dict[str, object] = {
            "role_id": role,
            "provider_profile_id": "deepseek_default",
            "model_id": model_id,
            "adapter_id": f"deepseek_{role}",
            "adapter_version": "v1",
            "base_endpoint": "https://api.deepseek.com",
            "prompt_schema_version": "deepseek-prompt-v1",
            "prompt_content_hash": deepseek_prompt_content_hash(
                role, enable_web_tools=(role == "agent" and enable_web_tools)
            ),
            "response_contract_version": deepseek_response_contract(
                role, enable_web_tools=(role == "agent" and enable_web_tools)
            ),
            "policy": policy,
            "credential_slot_id": "researchos_deepseek_api_key",
        }
        values.update(role_overrides.get(role, {}))
        models.append(RealModelSettings.model_validate(values))
    return RealIntegrationSettings(
        models=tuple(models),
        capabilities=tuple(item.pin() for item in capability_settings),
        capability_settings=capability_settings,
    )


def make_snapshot(
    settings: RealIntegrationSettings,
    *,
    run_id: str = "run_real",
    config_hash: str = "a" * 64,
) -> RealCompositionSnapshot:
    return RealCompositionSnapshot.build(
        SemanticCompositionPayload(
            run_id=run_id,
            run_config_hash=config_hash,
            model_bundles=tuple(item.bundle() for item in settings.models),
            capabilities=settings.capabilities,
            source_policy_id=settings.source_policy_id,
            source_policy_hash=settings.source_policy_hash,
        )
    )


def make_real_config(**values: object) -> RunConfig:
    """A REAL Run budget large enough for the frozen provider reservation."""

    values.setdefault(
        "budget_limits",
        BudgetLimits(max_tokens=1_500_000, max_cost_microunits=20_000_000),
    )
    return RunConfig(mode="real", **values)


def make_state(run_id: str = "run_real") -> RunState:
    config = make_real_config()
    now = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
    from researchos.domain.contracts import Budget, RunInput

    return RunState(
        run_id=run_id,
        revision=0,
        status=RunStatus.CREATED,
        input_snapshot=RunInput(query="real query"),
        input_hash=model_sha256(RunInput(query="real query")),
        config=config,
        config_hash=model_sha256(config),
        created_at=now,
        updated_at=now,
        budget=Budget(limits=config.budget_limits),
        last_transition_id="transition_one",
    )
