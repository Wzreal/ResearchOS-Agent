from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from phase9_fixtures import (
    FrozenClock,
    SequentialIds,
    make_real_config,
    make_settings,
    make_snapshot,
    make_state,
)
from pydantic import ValidationError

from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.real_composition_filesystem import (
    FilesystemRealCompositionStore,
)
from researchos.adapters.real_composition_memory import InMemoryRealCompositionStore
from researchos.adapters.real_run_probe import FilesystemPristineRealRunProbe
from researchos.application.errors import (
    RealCompositionConflict,
    RealCompositionCorruption,
    RealCompositionNotFound,
    RealRunBindingError,
    RunConfigurationError,
    RunNotFound,
)
from researchos.application.integration_factory import RealIntegrationFactory
from researchos.application.real_composition import RealCompositionManager
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import BudgetLimits, RunConfig, RunInput, RunStatus
from researchos.domain.identity import stable_hash, stable_id
from researchos.domain.real_composition import (
    CapabilityCompositionPin,
    ModelCallPolicySnapshot,
    RealCompositionSnapshot,
)
from researchos.domain.runtime import RuntimeCheckpoint


class DictSecrets:
    def __init__(self, value: str | None = "secret-value-one") -> None:
        self.value = value

    def get_secret(self, secret_id: str) -> str | None:
        del secret_id
        return self.value


@pytest.mark.parametrize("value", [None, "", " ", "\t\r\n"])
def test_generic_secret_source_rejects_missing_or_blank_values(value) -> None:
    manager, _, _, _, _ = _real_lifecycle(secrets=DictSecrets(value))
    with pytest.raises(RunConfigurationError, match="credential is absent"):
        manager.create(RunInput(query="real"), make_real_config())


def test_composition_identity_uses_non_circular_semantic_preimage() -> None:
    snapshot = make_snapshot(make_settings())
    expected_hash = stable_hash(snapshot.semantic.model_dump(mode="json"))
    assert snapshot.composition_hash == expected_hash
    assert snapshot.composition_id == stable_id(
        "realcomp",
        [snapshot.semantic.run_id, snapshot.semantic.run_config_hash, expected_hash],
    )
    dumped = snapshot.model_dump(mode="json")
    assert "composition_hash" not in dumped["semantic"]
    assert "composition_id" not in dumped["semantic"]


def test_valid_hash_with_wrong_composition_id_is_corruption() -> None:
    raw = make_snapshot(make_settings()).model_dump(mode="json")
    raw["composition_id"] = "realcomp_" + "b" * 64
    with pytest.raises(ValidationError, match="composition ID differs"):
        RealCompositionSnapshot.model_validate(raw)


def test_valid_looking_id_with_wrong_semantic_hash_is_corruption() -> None:
    raw = make_snapshot(make_settings()).model_dump(mode="json")
    raw["semantic"]["source_policy_hash"] = "c" * 64
    raw["composition_id"] = stable_id(
        "realcomp",
        [
            raw["semantic"]["run_id"],
            raw["semantic"]["run_config_hash"],
            raw["composition_hash"],
        ],
    )
    with pytest.raises(ValidationError, match="semantic hash differs"):
        RealCompositionSnapshot.model_validate(raw)


def test_nested_policy_hash_tamper_is_rejected() -> None:
    raw = make_snapshot(make_settings()).model_dump(mode="json")
    raw["semantic"]["model_bundles"][0]["policy"]["max_output_tokens"] += 1
    with pytest.raises(
        ValidationError,
        match="provider-call reservation differs|model call policy hash differs",
    ):
        RealCompositionSnapshot.model_validate(raw)


def test_nested_pricing_profile_currency_tamper_is_rejected() -> None:
    raw = make_snapshot(make_settings()).model_dump(mode="json")
    raw["semantic"]["model_bundles"][0]["policy"]["pricing_safety_profile"][
        "billing_currency"
    ] = "CNY"
    with pytest.raises(ValidationError, match="pricing safety profile hash differs"):
        RealCompositionSnapshot.model_validate(raw)


def test_nested_model_bundle_hash_tamper_is_rejected() -> None:
    raw = make_snapshot(make_settings()).model_dump(mode="json")
    raw["semantic"]["model_bundles"][0]["model_bundle_hash"] = "e" * 64
    with pytest.raises(ValidationError, match="model bundle hash differs"):
        RealCompositionSnapshot.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_output_tokens", 4_097),
        ("max_request_bytes", 1_048_577),
        ("max_response_bytes", 1_048_577),
        ("max_input_tokens", 32_769),
        ("connect_timeout_ms", 10_001),
        ("read_timeout_ms", 60_001),
        ("write_timeout_ms", 10_001),
        ("pool_timeout_ms", 10_001),
        ("max_cost_microunits_per_call", 11_000_001),
        ("pricing_policy_version", "v2"),
        ("input_cost_upper_bound_microunits_per_million_tokens", 10_000_001),
        ("output_cost_upper_bound_microunits_per_million_tokens", 20_000_001),
    ],
)
def test_every_policy_setting_changes_all_outer_identities(
    field: str, value: object
) -> None:
    baseline = make_snapshot(make_settings())
    changed = make_snapshot(make_settings(policy_overrides={field: value}))
    baseline_bundle = baseline.semantic.model_bundles[0]
    changed_bundle = changed.semantic.model_bundles[0]
    assert changed_bundle.policy.model_call_policy_hash != (
        baseline_bundle.policy.model_call_policy_hash
    )
    assert changed_bundle.model_bundle_hash != baseline_bundle.model_bundle_hash
    assert changed.composition_hash != baseline.composition_hash
    assert changed.composition_id != baseline.composition_id


@pytest.mark.parametrize(
    ("override", "value"),
    [("thinking_mode", "disabled"), ("reasoning_effort", "max")],
)
def test_deepseek_request_policy_changes_composition(
    override: str, value: str
) -> None:
    baseline = make_snapshot(make_settings())
    changed = make_snapshot(
        make_settings(policy_overrides={override: value})
    )
    assert changed.composition_hash != baseline.composition_hash
    assert changed.composition_id != baseline.composition_id


def test_thinking_mode_canonically_excludes_ignored_sampling_values() -> None:
    policy = make_settings().models[0].policy
    assert policy.temperature is None
    assert policy.top_p is None
    raw = policy.model_dump(mode="json")
    raw["temperature"] = "0"
    raw["model_call_policy_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="ignored sampling requires nulls"):
        ModelCallPolicySnapshot.model_validate(raw)


def test_effective_sampling_change_changes_disabled_thinking_identity() -> None:
    baseline = make_snapshot(
        make_settings(policy_overrides={"thinking_mode": "disabled"})
    )
    changed = make_snapshot(
        make_settings(
            policy_overrides={"thinking_mode": "disabled", "temperature": "0.1"}
        )
    )
    assert changed.composition_hash != baseline.composition_hash


def test_role_model_pricing_policy_is_bound_to_each_model() -> None:
    settings = make_settings()
    agent = settings.model_for_role("agent")
    planning = settings.model_for_role("planning")
    assert agent.policy.priced_model_id == "deepseek-v4-flash"
    assert planning.policy.priced_model_id == "deepseek-v4-pro"
    assert agent.policy.model_call_policy_hash != planning.policy.model_call_policy_hash


def test_pricing_policy_for_another_model_is_rejected() -> None:
    settings = make_settings()
    planning = settings.model_for_role("planning")
    with pytest.raises(ValidationError, match="pricing upper-bound policy model"):
        type(planning).model_validate(
            {**planning.model_dump(mode="json"), "model_id": "deepseek-v4-flash"}
        )


def test_seed_generation_control_changes_identity() -> None:
    baseline = make_snapshot(make_settings())
    changed = make_snapshot(
        make_settings(
            policy_overrides={
                "seed": 7,
                "deterministic_generation": "provider_seed",
            }
        )
    )
    assert changed.composition_hash != baseline.composition_hash


def test_credential_slot_identity_changes_bundle_and_composition() -> None:
    baseline_settings = make_settings()
    changed_settings = make_settings(
        role_overrides={
            "planning": {"credential_slot_id": "researchos_deepseek_secondary_key"}
        }
    )
    baseline = make_snapshot(baseline_settings)
    changed = make_snapshot(changed_settings)
    assert (
        baseline_settings.model_for_role("planning").bundle().model_bundle_hash
        != changed_settings.model_for_role("planning").bundle().model_bundle_hash
    )
    assert baseline.composition_hash != changed.composition_hash


def test_model_policy_rejects_inconsistent_pricing_rules_hash() -> None:
    raw = make_settings().models[0].policy.model_dump(mode="json")
    raw["pricing_rules_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="pricing rules hash differs"):
        ModelCallPolicySnapshot.model_validate(raw)


@pytest.mark.parametrize(
    "field",
    [
        "input_cost_upper_bound_microunits_per_million_tokens",
        "output_cost_upper_bound_microunits_per_million_tokens",
    ],
)
def test_missing_pricing_upper_bound_cannot_be_encoded_as_zero(field: str) -> None:
    with pytest.raises(ValueError):
        make_settings(policy_overrides={field: 0})


def test_filesystem_same_snapshot_concurrency_is_idempotent(tmp_path) -> None:
    clock = FrozenClock()
    snapshot = make_snapshot(make_settings())
    stores = tuple(
        FilesystemRealCompositionStore(tmp_path / ".", clock=clock)
        for _ in range(8)
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda store: store.create(snapshot), stores))
    assert len({item.artifact_content_hash for item in results}) == 1
    assert len({item.recorded_at for item in results}) == 1


def test_filesystem_conflicting_concurrency_has_one_immutable_winner(tmp_path) -> None:
    clock = FrozenClock()
    snapshots = (
        make_snapshot(make_settings()),
        make_snapshot(
            make_settings(policy_overrides={"max_output_tokens": 4_097})
        ),
    )
    stores = tuple(
        FilesystemRealCompositionStore(tmp_path, clock=clock) for _ in snapshots
    )

    def create(index: int):
        try:
            return stores[index].create(snapshots[index])
        except RealCompositionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(create, range(2)))
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RealCompositionConflict) for item in results) == 1
    persisted = stores[0].load("run_real").snapshot
    assert persisted in snapshots


def test_recorded_at_is_owned_by_first_write(tmp_path) -> None:
    clock = FrozenClock()
    store = FilesystemRealCompositionStore(tmp_path, clock=clock)
    snapshot = make_snapshot(make_settings())
    first = store.create(snapshot)
    clock.advance(hours=1)
    replay = store.create(snapshot)
    assert replay == first
    assert replay.recorded_at != clock.now()


def test_persisted_composition_contains_no_credential_or_endpoint(tmp_path) -> None:
    store = FilesystemRealCompositionStore(tmp_path, clock=FrozenClock())
    store.create(make_snapshot(make_settings()))
    encoded = (tmp_path / "run_real" / "real_composition.json").read_text(
        encoding="utf-8"
    )
    assert "credential-canary-value" not in encoded
    assert "api.deepseek.com" not in encoded
    assert "authorization" not in encoded.lower()


def test_credential_canary_never_enters_run_trace_or_composition_authority() -> None:
    canary = "phase9-credential-canary-value"
    manager, _, compositions, runs, trace = _real_lifecycle(
        secrets=DictSecrets(canary)
    )
    state = manager.create(RunInput(query="real"), make_real_config())
    persisted = "\n".join(
        (
            runs.load(state.run_id).model_dump_json(),
            compositions.load(state.run_id).model_dump_json(),
            *(event.model_dump_json() for event in trace.read(state.run_id)),
        )
    )
    assert canary not in persisted
    assert "credential" not in json.dumps(
        RuntimeCheckpoint.model_json_schema(), sort_keys=True
    ).lower()


def test_filesystem_tamper_is_corruption(tmp_path) -> None:
    store = FilesystemRealCompositionStore(tmp_path, clock=FrozenClock())
    store.create(make_snapshot(make_settings()))
    path = tmp_path / "run_real" / "real_composition.json"
    raw = json.loads(path.read_bytes())
    raw["snapshot"]["composition_id"] = "realcomp_" + "d" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RealCompositionCorruption):
        store.load("run_real")


def test_filesystem_noncanonical_bytes_are_corruption(tmp_path) -> None:
    store = FilesystemRealCompositionStore(tmp_path, clock=FrozenClock())
    envelope = store.create(make_snapshot(make_settings()))
    path = tmp_path / "run_real" / "real_composition.json"
    path.write_text(
        json.dumps(envelope.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    with pytest.raises(RealCompositionCorruption, match="canonical bytes"):
        store.load("run_real")


def _real_lifecycle(
    *, settings=None, secrets=None, composition_store=None, run_store=None, trace=None
):
    clock = FrozenClock()
    settings = settings or make_settings()
    composition_store = composition_store or InMemoryRealCompositionStore(clock=clock)
    run_store = run_store or InMemoryRunStore()
    trace = trace or InMemoryTraceSink()
    guard = RealCompositionManager(
        settings=settings,
        secrets=secrets or DictSecrets(),
        store=composition_store,
    )
    manager = RunManager(
        store=run_store,
        trace_sink=trace,
        clock=clock,
        id_factory=SequentialIds(),
        integration_guard=guard,
    )
    return manager, guard, composition_store, run_store, trace


def test_real_run_create_binds_before_planning() -> None:
    manager, _, _, _, _ = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    assert manager.transition(state.run_id, RunStatus.PLANNING).status is (
        RunStatus.PLANNING
    )


@pytest.mark.parametrize(
    "budget",
    [
        BudgetLimits(max_tokens=1_000_000, max_cost_microunits=20_000_000),
        BudgetLimits(max_tokens=1_500_000, max_cost_microunits=10_000_000),
    ],
)
def test_run_budget_below_provider_reservation_fails_before_create(
    budget: BudgetLimits,
) -> None:
    manager, _, _, runs, trace = _real_lifecycle()
    with pytest.raises(
        RunConfigurationError, match="provider-call reservation exceeds"
    ):
        manager.create(
            RunInput(query="real"),
            RunConfig(mode="real", budget_limits=budget),
        )
    with pytest.raises(RunNotFound):
        runs.load("run_1")
    assert trace.read("run_1") == ()


def test_post_create_binding_failure_preserves_created_run_id() -> None:
    class FailingGuard:
        def validate_create(self, config):
            del config

        def bind_created(self, state):
            del state
            raise OSError("binding failed")

        def validate_resume(self, state):
            del state

        def validate_bound_state(self, state):
            del state

        def validate_transition(self, state, target):
            del state, target

    runs = InMemoryRunStore()
    manager = RunManager(
        store=runs,
        trace_sink=InMemoryTraceSink(),
        clock=FrozenClock(),
        id_factory=SequentialIds(),
        integration_guard=FailingGuard(),
    )
    with pytest.raises(RealRunBindingError) as caught:
        manager.create(RunInput(query="real"), make_real_config())
    assert caught.value.run_id == "run_1"
    assert caught.value.run_state_committed is True
    assert runs.load(caught.value.run_id).status is RunStatus.CREATED


def test_deleted_composition_blocks_existing_run_mutation() -> None:
    manager, _, compositions, _, _ = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    manager.transition(state.run_id, RunStatus.PLANNING)
    compositions._items.pop(state.run_id)
    with pytest.raises(RealCompositionNotFound):
        manager.transition(state.run_id, RunStatus.READY)
    with pytest.raises(RealCompositionNotFound):
        manager.finalize(state.run_id, RunStatus.CANCELLED, reason="cancelled")


def test_changed_policy_blocks_mutation_without_resume() -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    manager.transition(state.run_id, RunStatus.PLANNING)
    changed_manager, _, _, _, _ = _real_lifecycle(
        settings=make_settings(policy_overrides={"read_timeout_ms": 60_001}),
        composition_store=compositions,
        run_store=runs,
        trace=trace,
    )
    with pytest.raises(RunConfigurationError, match="differs from current settings"):
        changed_manager.transition(state.run_id, RunStatus.READY)


def test_credential_rotation_does_not_change_composition_or_block_mutation() -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    manager.transition(state.run_id, RunStatus.PLANNING)
    rotated, _, _, _, _ = _real_lifecycle(
        secrets=DictSecrets("rotated-secret-value"),
        composition_store=compositions,
        run_store=runs,
        trace=trace,
    )
    assert rotated.transition(state.run_id, RunStatus.READY).status is RunStatus.READY


def test_missing_credential_does_not_block_safe_terminal_mutation() -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    manager.transition(state.run_id, RunStatus.PLANNING)
    without_secret, _, _, _, _ = _real_lifecycle(
        secrets=DictSecrets(None),
        composition_store=compositions,
        run_store=runs,
        trace=trace,
    )
    terminal = without_secret.finalize(
        state.run_id, RunStatus.CANCELLED, reason="operator cancellation"
    )
    assert terminal.status is RunStatus.CANCELLED


@pytest.mark.parametrize(
    "settings",
    [
        make_settings(role_overrides={"planning": {"model_id": "deepseek-v4-flash"}}),
        make_settings(
            role_overrides={"planning": {"prompt_content_hash": "f" * 64}}
        ),
        make_settings(
            role_overrides={
                "planning": {"base_endpoint": "https://api.deepseek.com/v2"}
            }
        ),
        make_settings(policy_overrides={"read_timeout_ms": 60_001}),
        make_settings(
            role_overrides={
                "planning": {
                    "credential_slot_id": "researchos_deepseek_secondary_key"
                }
            }
        ),
    ],
)
def test_changed_model_prompt_endpoint_or_policy_blocks_resume(settings) -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    changed, _, _, _, _ = _real_lifecycle(
        settings=settings,
        composition_store=compositions,
        run_store=runs,
        trace=trace,
    )
    with pytest.raises(RunConfigurationError, match="differs from current settings"):
        changed.resume(
            state.run_id,
            expected_input=RunInput(query="real"),
            expected_config=make_real_config(),
        )


def test_resume_after_credential_rotation_rebinds_created_run() -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    resumed, _, _, _, _ = _real_lifecycle(
        secrets=DictSecrets("rotated-secret-value"),
        composition_store=compositions,
        run_store=runs,
        trace=trace,
    )
    resumed.resume(
        state.run_id,
        expected_input=RunInput(query="real"),
        expected_config=make_real_config(),
    )
    assert resumed.transition(state.run_id, RunStatus.PLANNING).status is (
        RunStatus.PLANNING
    )


def test_missing_composition_bootstraps_only_pristine_created_run(tmp_path) -> None:
    manager, _, compositions, runs, trace = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    compositions._items.pop(state.run_id)
    probe = FilesystemPristineRealRunProbe(tmp_path, trace_sink=trace)
    replacement_store = InMemoryRealCompositionStore(clock=FrozenClock())
    guard = RealCompositionManager(
        settings=make_settings(),
        secrets=DictSecrets(),
        store=replacement_store,
        pristine_probe=probe,
    )
    replacement = RunManager(
        store=runs,
        trace_sink=trace,
        clock=FrozenClock(),
        id_factory=SequentialIds(),
        integration_guard=guard,
    )
    replacement.resume(
        state.run_id,
        expected_input=RunInput(query="real"),
        expected_config=make_real_config(),
    )
    assert replacement_store.load(state.run_id).snapshot.semantic.run_id == state.run_id


def test_tampered_composition_blocks_verifying_mutation(tmp_path) -> None:
    clock = FrozenClock()
    compositions = FilesystemRealCompositionStore(tmp_path, clock=clock)
    runs = InMemoryRunStore()
    trace = InMemoryTraceSink()
    guard = RealCompositionManager(
        settings=make_settings(), secrets=DictSecrets(), store=compositions
    )
    manager = RunManager(
        store=runs,
        trace_sink=trace,
        clock=clock,
        id_factory=SequentialIds(),
        integration_guard=guard,
    )
    state = manager.create(RunInput(query="real"), make_real_config())
    for status in (
        RunStatus.PLANNING,
        RunStatus.READY,
        RunStatus.RUNNING,
        RunStatus.VERIFYING,
    ):
        state = manager.transition(state.run_id, status)
    path = tmp_path / state.run_id / "real_composition.json"
    raw = json.loads(path.read_bytes())
    raw["artifact_content_hash"] = "f" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RealCompositionCorruption):
        manager.transition(state.run_id, RunStatus.EVALUATING)


def test_unbound_factory_rejects_adapter_construction() -> None:
    clock = FrozenClock()
    state_config = make_real_config()
    settings = make_settings()
    store = InMemoryRealCompositionStore(clock=clock)
    from researchos.domain.contracts import model_sha256

    store.create(
        make_snapshot(
            settings, run_id="run_real", config_hash=model_sha256(state_config)
        )
    )
    manager = RealCompositionManager(
        settings=settings, secrets=DictSecrets(), store=store
    )
    runs = InMemoryRunStore()
    runs.create(make_state().model_copy(update={"status": RunStatus.RUNNING}))
    with pytest.raises(RunConfigurationError, match="process-local binding"):
        RealIntegrationFactory(manager, run_store=runs).agent("run_real")


def test_phase9a_real_capabilities_fail_closed_before_secret_or_dispatch() -> None:
    settings = make_settings().model_copy(
        update={
            "capabilities": (
                CapabilityCompositionPin(
                    capability_id="python",
                    adapter_id="python_subprocess",
                    operation_version="v1",
                    tool_id="python_tool",
                ),
            )
        }
    )
    guard = RealCompositionManager(
        settings=settings,
        secrets=DictSecrets(),
        store=InMemoryRealCompositionStore(clock=FrozenClock()),
    )
    with pytest.raises(RunConfigurationError, match="deferred until Phase 9B"):
        guard.validate_create(
            make_real_config(allowed_capability_ids=("python",))
        )


def test_factory_enforces_role_specific_run_lifecycle_before_transport() -> None:
    manager, guard, _, runs, _ = _real_lifecycle()
    state = manager.create(RunInput(query="real"), make_real_config())
    transports: list[object] = []

    def transport_factory(bound):
        del bound
        transport = object()
        transports.append(transport)
        return transport

    factory = RealIntegrationFactory(
        guard, run_store=runs, transport_factory=transport_factory
    )
    with pytest.raises(RunConfigurationError, match="planning dispatch"):
        factory.planning_model(state.run_id)
    with pytest.raises(RunConfigurationError, match="agent dispatch"):
        factory.agent(state.run_id)
    assert transports == []

    manager.transition(state.run_id, RunStatus.PLANNING)
    assert factory.planning_model(state.run_id) is not None
    with pytest.raises(RunConfigurationError, match="agent dispatch"):
        factory.agent(state.run_id)

    manager.transition(state.run_id, RunStatus.READY)
    manager.transition(state.run_id, RunStatus.RUNNING)
    assert factory.agent(state.run_id) is not None
    with pytest.raises(RunConfigurationError, match="planning dispatch"):
        factory.planning_model(state.run_id)
    assert factory.trusted_runtime_replanning_model(state.run_id) is not None

    manager.transition(state.run_id, RunStatus.VERIFYING)
    assert factory.verification_model(state.run_id) is not None
    with pytest.raises(RunNotFound):
        factory.agent("run_wrong")
    assert len(transports) == 4
