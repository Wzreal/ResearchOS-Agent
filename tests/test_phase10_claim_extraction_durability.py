from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from conftest import FrozenClock
from runtime_fixtures import runtime_example

from researchos.adapters.claim_extraction_operation_filesystem import (
    FilesystemClaimExtractionOperationStore,
)
from researchos.adapters.claim_extraction_operation_memory import (
    InMemoryClaimExtractionOperationStore,
)
from researchos.adapters.claim_memory import InMemoryClaimGraphStore
from researchos.adapters.evidence_memory import InMemoryEvidenceStore
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_claim_extraction import (
    ClaimExtractionFixtureKey,
    MockClaimExtractionModel,
)
from researchos.application.claim_extraction_operation import (
    ClaimExtractionOperationManager,
)
from researchos.application.claim_extractor import ClaimExtractor
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.errors import (
    ClaimExtractionOperationAlreadyExists,
    ClaimExtractionOperationRevisionConflict,
    ClaimGraphNotFound,
    CorruptClaimExtractionOperation,
    PlanningModelFailure,
)
from researchos.domain.claim_extraction import (
    ClaimExtractionModelResult,
    ClaimExtractionPolicy,
    ClaimExtractionResponse,
)
from researchos.domain.claim_extraction_operation import (
    ClaimExtractionOperation,
    ClaimExtractionOperationStatus,
)
from researchos.domain.claims import ClaimEvidenceRelation, ClaimGenerationContext
from researchos.domain.contracts import canonical_json_bytes, model_sha256
from researchos.domain.evidence import (
    EvidenceRecord,
    EvidenceRevision,
    EvidenceStoreSnapshot,
    SourceRecord,
    SourceType,
)
from researchos.domain.identity import (
    normalize_content,
    sha256_text,
    stable_hash,
    stable_id,
)
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty


def _operation(
    *, run_id: str = "run_1", extraction_id: str = "extract_1"
) -> ClaimExtractionOperation:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    return ClaimExtractionOperation(
        operation_id="ceop_1",
        run_id=run_id,
        run_revision=3,
        extraction_id=extraction_id,
        policy_hash="0" * 64,
        model_bundle_hash="1" * 64,
        evidence_snapshot_hash="2" * 64,
        request_hash="3" * 64,
        workflow_budget_slice_hash="4" * 64,
        created_at=now,
        updated_at=now,
    )


def test_filesystem_operation_round_trip_cas_and_duplicate(tmp_path) -> None:
    store = FilesystemClaimExtractionOperationStore(tmp_path)
    operation = _operation()
    store.create(operation)
    assert store.load("run_1", "extract_1") == operation
    with pytest.raises(ClaimExtractionOperationAlreadyExists):
        store.create(operation)
    updated = operation.model_copy(
        update={"checkpoint_revision": 1, "updated_at": operation.updated_at}
    )
    store.save(updated, expected_revision=0)
    with pytest.raises(ClaimExtractionOperationRevisionConflict):
        store.save(updated, expected_revision=0)


def test_historical_claim_extraction_operation_hash_and_bytes_stay_v1_stable(
    tmp_path,
) -> None:
    operation = _operation()
    assert model_sha256(operation) == (
        "a0e4395f83d6fc15121af614d3ba7ff1c5125616aa14332b26a4cd9739145c55"
    )
    historical = {
        "envelope_version": 1,
        "operation": operation.model_dump(mode="json"),
        "payload_sha256": model_sha256(operation),
    }
    path = tmp_path / "run_1" / "claim_extraction_operations" / "extract_1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        json.dumps(historical, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    )
    loaded = FilesystemClaimExtractionOperationStore(tmp_path).load(
        "run_1", "extract_1"
    )
    assert loaded == operation
    assert model_sha256(loaded) == historical["payload_sha256"]
    assert canonical_json_bytes(loaded) == canonical_json_bytes(operation)


def test_claim_extraction_diagnostics_are_durable_but_excluded_from_v1_payload_hash(
    tmp_path,
) -> None:
    operation = ClaimExtractionOperation.model_validate(
        {
            **_operation().model_dump(mode="python"),
            "provider_diagnostics": {
                "schema_version": "provider_diagnostics_v1",
                "role_id": "claim_extraction",
                "operation_id": "extract_1",
                "dispatch_classification": "response_received",
                "http_response_received": True,
                "provider_finish_reason": "stop",
                "input_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 5,
                "max_input_tokens": 10,
                "max_output_tokens": 10,
                "canonical_request_bytes": 100,
                "error_code": None,
                "retryable": False,
                "duration_milliseconds": 1,
            },
        }
    )
    assert model_sha256(operation) == model_sha256(_operation())
    store = FilesystemClaimExtractionOperationStore(tmp_path)
    store.create(operation)
    persisted = next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")
    assert '"provider_diagnostics"' in persisted
    assert store.load("run_1", "extract_1") == operation


def test_claim_extraction_diagnostics_reject_secret_or_raw_content_before_journal(
    tmp_path,
) -> None:
    with pytest.raises(ValueError):
        ClaimExtractionOperation.model_validate(
            {
                **_operation().model_dump(mode="python"),
                "provider_diagnostics": {
                    "schema_version": "provider_diagnostics_v1",
                    "role_id": "planning",
                    "dispatch_classification": "response_received",
                    "http_response_received": True,
                    "raw_prompt": "PROMPT_SENTINEL",
                },
            }
        )
    assert not list(tmp_path.rglob("*.json"))


def test_filesystem_operation_rejects_tampered_payload(tmp_path) -> None:
    store = FilesystemClaimExtractionOperationStore(tmp_path)
    store.create(_operation())
    path = tmp_path / "run_1" / "claim_extraction_operations" / "extract_1.json"
    raw = path.read_text(encoding="utf-8").replace(
        '"payload_sha256":"', '"payload_sha256":"f'
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(CorruptClaimExtractionOperation):
        store.load("run_1", "extract_1")


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"operation":{}}',
        b'{"envelope_version":99,"operation":{},"payload_sha256":"0"}',
    ],
)
def test_filesystem_operation_rejects_malformed_or_torn_bytes(tmp_path, raw) -> None:
    store = FilesystemClaimExtractionOperationStore(tmp_path)
    store.create(_operation())
    path = tmp_path / "run_1" / "claim_extraction_operations" / "extract_1.json"
    path.write_bytes(raw)

    with pytest.raises(CorruptClaimExtractionOperation):
        store.load("run_1", "extract_1")


def test_filesystem_operation_load_rejects_run_or_extraction_identity_mismatch(
    tmp_path,
) -> None:
    store = FilesystemClaimExtractionOperationStore(tmp_path)
    store.create(_operation())
    original = (
        tmp_path / "run_1" / "claim_extraction_operations" / "extract_1.json"
    )
    wrong_run = (
        tmp_path / "run_other" / "claim_extraction_operations" / "extract_1.json"
    )
    wrong_run.parent.mkdir(parents=True)
    wrong_run.write_bytes(original.read_bytes())
    wrong_extraction = (
        tmp_path / "run_1" / "claim_extraction_operations" / "extract_other.json"
    )
    wrong_extraction.write_bytes(original.read_bytes())

    with pytest.raises(CorruptClaimExtractionOperation):
        store.load("run_other", "extract_1")
    with pytest.raises(CorruptClaimExtractionOperation):
        store.load("run_1", "extract_other")


def test_filesystem_operation_rejects_unsafe_path_identity(tmp_path) -> None:
    store = FilesystemClaimExtractionOperationStore(tmp_path)

    with pytest.raises(ValueError, match="unsafe"):
        store.load("../run", "extract_1")


def _extractor_fixture():
    example = runtime_example()
    clock = FrozenClock(example.clock.now())
    evidence = InMemoryEvidenceStore()
    content = "Evidence body"
    source_id = stable_id(
        "src", [example.state.run_id, SourceType.WEB.value, "https://example.test/"]
    )
    evidence_id = stable_id(
        "ev", [example.state.run_id, source_id, "body_1", "text/plain"]
    )
    evidence.create(
        EvidenceStoreSnapshot(
            run_id=example.state.run_id,
            store_revision=0,
            sources=(
                SourceRecord(
                    run_id=example.state.run_id,
                    source_id=source_id,
                    source_type=SourceType.WEB,
                    canonical_locator="https://example.test/",
                    original_locator="https://example.test/",
                    first_seen_at=clock.now(),
                    first_adapter_id="mock_browser",
                ),
            ),
            evidence=(
                EvidenceRecord(
                    run_id=example.state.run_id,
                    evidence_id=evidence_id,
                    source_id=source_id,
                    evidence_scope_key="body_1",
                    media_type="text/plain",
                    current_revision=1,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                ),
            ),
            revisions=(
                EvidenceRevision(
                    run_id=example.state.run_id,
                    evidence_id=evidence_id,
                    revision=1,
                    content=content,
                    content_hash=sha256_text(content),
                    normalized_content_hash=sha256_text(normalize_content(content)),
                    extraction_context_hash=stable_hash({}),
                    extractor_id="mock_browser",
                    extractor_version="v1",
                    created_at=clock.now(),
                ),
            ),
        )
    )
    graph = ClaimGraphService(
        store=InMemoryClaimGraphStore(),
        evidence_store=evidence,
        clock=clock,
        trace_sink=InMemoryTraceSink(),
    )
    operations = InMemoryClaimExtractionOperationStore()
    manager = ClaimExtractionOperationManager(store=operations, clock=clock)
    policy = ClaimExtractionPolicy(
        max_evidence_items=1,
        max_context_bytes=1000,
        max_claims=1,
        max_references_per_claim=1,
        max_statement_bytes=1000,
        max_response_bytes=1000,
    )
    snapshot_hash = stable_hash(
        evidence.load(example.state.run_id).model_dump(mode="json")
    )
    response = ClaimExtractionResponse(
        run_id=example.state.run_id,
        extraction_id="extract_1",
        candidates=(
            {
                "ordinal": 1,
                "statement": "A claim",
                "evidence_pins": [
                    {
                        "evidence_id": evidence_id,
                        "evidence_revision": 1,
                        "relation": ClaimEvidenceRelation.SUPPORTS,
                    }
                ],
            },
        ),
    )
    model = MockClaimExtractionModel(
        {ClaimExtractionFixtureKey(snapshot_hash): response}
    )
    service = ClaimExtractor(
        operations=operations,
        operation_manager=manager,
        evidence_store=evidence,
        graph=graph,
        model=model,
        clock=clock,
    )
    return example, operations, graph, model, service, policy


def test_prepared_extraction_dispatches_once_and_completes() -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    operation = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert operation.status.value == "completed"
    assert model.invocation_count == 1
    assert len(graph.list_claims(example.state.run_id)) == 1
    assert operations.load(example.state.run_id, "extract_1").status == operation.status


def _validated_operation(service, manager, state, policy, response):
    request = service._request(state, "extract_1", policy)
    operation = service._load_or_prepare(
        state, request, workflow_budget_slice_hash="5" * 64
    )
    dispatched = manager.transition(
        operation, ClaimExtractionOperationStatus.DISPATCHED
    )
    return manager.transition(
        dispatched,
        ClaimExtractionOperationStatus.VALIDATED,
        response=response,
        mutation_keys=service._mutation_keys(dispatched, response),
    )


def test_dispatched_restart_becomes_unknown_without_second_model_call() -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    manager = ClaimExtractionOperationManager(store=operations, clock=service._clock)
    request = service._request(example.state, "extract_1", policy)
    operation = service._load_or_prepare(
        example.state, request, workflow_budget_slice_hash="5" * 64
    )
    manager.transition(operation, ClaimExtractionOperationStatus.DISPATCHED)

    recovered = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert recovered.status is ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN
    assert model.invocation_count == 0
    with pytest.raises(ClaimGraphNotFound):
        graph.list_claims(example.state.run_id)
    assert (
        service.execute(
            example.state,
            extraction_id="extract_1",
            policy=policy,
            workflow_budget_slice_hash="5" * 64,
        ).status
        is ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN
    )
    assert model.invocation_count == 0


def test_validated_restart_replays_graph_without_model_call() -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    manager = ClaimExtractionOperationManager(store=operations, clock=service._clock)
    response = next(iter(model._fixtures.values()))
    _validated_operation(service, manager, example.state, policy, response)

    completed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert completed.status is ClaimExtractionOperationStatus.COMPLETED
    assert model.invocation_count == 0
    assert len(graph.list_claims(example.state.run_id)) == 1


def test_validated_restart_skips_claim_receipt_and_applies_missing_edge() -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    manager = ClaimExtractionOperationManager(store=operations, clock=service._clock)
    response = next(iter(model._fixtures.values()))
    operation = _validated_operation(
        service, manager, example.state, policy, response
    )
    candidate = response.candidates[0]
    create_key = stable_hash(
        ["claim-extraction-create-v1", operation.operation_id, candidate.ordinal]
    )
    graph.create_claim(
        run_id=example.state.run_id,
        run_revision=example.state.revision,
        claim_scope_key=stable_id(
            "scope", [operation.extraction_id, candidate.ordinal]
        ),
        statement=candidate.statement,
        generation_context=ClaimGenerationContext(
            run_revision=example.state.revision,
            producer_id="claim_extraction",
            source_operation_key=operation.request_hash,
        ),
        operation_key=create_key,
    )
    before = graph._store.load(example.state.run_id)

    completed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    after = graph._store.load(example.state.run_id)

    assert completed.status is ClaimExtractionOperationStatus.COMPLETED
    assert model.invocation_count == 0
    assert len(after.claims) == len(before.claims)
    assert len(after.claim_revisions) == len(before.claim_revisions)
    assert len(after.receipts) == len(before.receipts) + 1
    assert len(after.edge_revisions) == 1


def test_completed_restart_has_zero_model_calls_and_zero_graph_writes() -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    completed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    snapshot = graph._store.load(example.state.run_id)
    again = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert again == completed
    assert model.invocation_count == 1
    assert graph._store.load(example.state.run_id) == snapshot


def test_successful_exact_settlement_survives_completed_and_restart() -> None:
    example, operations, _, _, service, policy = _extractor_fixture()
    completed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert completed.usage == RuntimeResourceAmount()
    assert completed.usage_certainty is UsageCertainty.EXACT
    restarted = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert restarted.usage == RuntimeResourceAmount()
    assert restarted.usage_certainty is UsageCertainty.EXACT
    assert operations.load(example.state.run_id, "extract_1") == restarted


def test_upper_bound_settlement_survives_completed() -> None:
    example, operations, _, model, service, policy = _extractor_fixture()
    manager = ClaimExtractionOperationManager(store=operations, clock=service._clock)
    response = next(iter(model._fixtures.values()))
    request = service._request(example.state, "extract_1", policy)
    prepared = service._load_or_prepare(
        example.state, request, workflow_budget_slice_hash="5" * 64
    )
    dispatched = manager.transition(prepared, ClaimExtractionOperationStatus.DISPATCHED)
    validated = manager.transition(
        dispatched,
        ClaimExtractionOperationStatus.VALIDATED,
        response=response,
        usage=RuntimeResourceAmount(tokens=7),
        usage_certainty=UsageCertainty.UPPER_BOUND,
        mutation_keys=service._mutation_keys(dispatched, response),
    )
    completed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert validated.usage_certainty is UsageCertainty.UPPER_BOUND
    assert completed.usage == RuntimeResourceAmount(tokens=7)
    assert completed.usage_certainty is UsageCertainty.UPPER_BOUND


def test_unknown_settlement_has_no_amount() -> None:
    example, _, _, model, _, _ = _extractor_fixture()
    response = next(iter(model._fixtures.values()))
    assert ClaimExtractionModelResult(
        response=response, usage_certainty=UsageCertainty.UNKNOWN
    ).usage is None
    with pytest.raises(ValueError, match="unknown usage"):
        ClaimExtractionModelResult(
            response=response,
            usage=RuntimeResourceAmount(),
            usage_certainty=UsageCertainty.UNKNOWN,
        )


def test_known_provider_failure_settlement_is_durable() -> None:
    example, operations, _, model, service, policy = _extractor_fixture()
    key = next(iter(model._fixtures))
    model._fixtures = {
        key: PlanningModelFailure(
            "provider_failed",
            "provider failed",
            usage=RuntimeResourceAmount(tokens=3),
            usage_certainty=UsageCertainty.UPPER_BOUND,
        )
    }
    failed = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert failed.status is ClaimExtractionOperationStatus.FAILED
    assert failed.usage == RuntimeResourceAmount(tokens=3)
    assert failed.usage_certainty is UsageCertainty.UPPER_BOUND
    assert operations.load(example.state.run_id, "extract_1") == failed


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        (ClaimExtractionOperationStatus.FAILED, "claim_extraction_failed"),
        (ClaimExtractionOperationStatus.CANCELLED, "claim_extraction_cancelled"),
        (
            ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN,
            "claim_extraction_interrupted_unknown",
        ),
    ],
)
def test_terminal_operation_restart_never_retries(
    status: ClaimExtractionOperationStatus, failure_code: str
) -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    manager = ClaimExtractionOperationManager(store=operations, clock=service._clock)
    request = service._request(example.state, "extract_1", policy)
    operation = service._load_or_prepare(
        example.state, request, workflow_budget_slice_hash="5" * 64
    )
    if status is ClaimExtractionOperationStatus.INTERRUPTED_UNKNOWN:
        operation = manager.transition(
            operation, ClaimExtractionOperationStatus.DISPATCHED
        )
    manager.transition(operation, status, failure_code=failure_code)

    assert (
        service.execute(
            example.state,
            extraction_id="extract_1",
            policy=policy,
            workflow_budget_slice_hash="5" * 64,
        ).status
        is status
    )
    assert model.invocation_count == 0
    with pytest.raises(ClaimGraphNotFound):
        graph.list_claims(example.state.run_id)


def _response_payload(example, *, candidate: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": example.state.run_id,
        "extraction_id": "extract_1",
        "candidates": [candidate],
    }


@pytest.mark.parametrize(
    "fixture",
    [
        {"malformed": True},
        lambda example, evidence_id: _response_payload(
            example,
            candidate={
                "ordinal": 1,
                "statement": "unknown evidence",
                "evidence_pins": [
                    {
                        "evidence_id": "evidence_unknown",
                        "evidence_revision": 1,
                        "relation": "supports",
                    }
                ],
            },
        ),
        lambda example, evidence_id: _response_payload(
            example,
            candidate={
                "ordinal": 1,
                "statement": "stale evidence",
                "evidence_pins": [
                    {
                        "evidence_id": evidence_id,
                        "evidence_revision": 99,
                        "relation": "supports",
                    }
                ],
            },
        ),
        lambda example, evidence_id: {
            **_response_payload(
                example,
                candidate={
                    "ordinal": 1,
                    "statement": "cross run",
                    "evidence_pins": [
                        {
                            "evidence_id": evidence_id,
                            "evidence_revision": 1,
                            "relation": "supports",
                        }
                    ],
                },
            ),
            "run_id": "run_other",
        },
        lambda example, evidence_id: _response_payload(
            example,
            candidate={
                "ordinal": 1,
                "statement": "duplicate pin",
                "evidence_pins": [
                    {
                        "evidence_id": evidence_id,
                        "evidence_revision": 1,
                        "relation": "supports",
                    },
                    {
                        "evidence_id": evidence_id,
                        "evidence_revision": 1,
                        "relation": "contextualizes",
                    },
                ],
            },
        ),
        lambda example, evidence_id: _response_payload(
            example,
            candidate={
                "ordinal": 1,
                "statement": "no support",
                "evidence_pins": [
                    {
                        "evidence_id": evidence_id,
                        "evidence_revision": 1,
                        "relation": "contradicts",
                    }
                ],
            },
        ),
    ],
)
def test_invalid_model_response_never_mutates_claim_graph(fixture) -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    evidence_id = service._evidence.load(example.state.run_id).evidence[0].evidence_id
    model._fixtures = {
        ClaimExtractionFixtureKey(
            stable_hash(service._evidence.load(example.state.run_id).model_dump(mode="json"))
        ): fixture(example, evidence_id) if callable(fixture) else fixture
    }

    operation = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert operation.status is ClaimExtractionOperationStatus.FAILED
    assert model.invocation_count == 1
    with pytest.raises(ClaimGraphNotFound):
        graph._store.load(example.state.run_id)


@pytest.mark.parametrize("fixture_kind", ["oversized", "model_failure"])
def test_oversized_or_model_failure_never_mutates_claim_graph(
    fixture_kind: str,
) -> None:
    example, operations, graph, model, service, policy = _extractor_fixture()
    evidence_id = service._evidence.load(example.state.run_id).evidence[0].evidence_id
    key = ClaimExtractionFixtureKey(
        stable_hash(service._evidence.load(example.state.run_id).model_dump(mode="json"))
    )
    if fixture_kind == "oversized":
        model._fixtures = {
            key: _response_payload(
                example,
                candidate={
                    "ordinal": 1,
                    "statement": "x" * (policy.max_statement_bytes + 1),
                    "evidence_pins": [
                        {
                            "evidence_id": evidence_id,
                            "evidence_revision": 1,
                            "relation": "supports",
                        }
                    ],
                },
            )
        }
    else:
        model._fixtures = {key: RuntimeError("model failed")}

    operation = service.execute(
        example.state,
        extraction_id="extract_1",
        policy=policy,
        workflow_budget_slice_hash="5" * 64,
    )
    assert operation.status is ClaimExtractionOperationStatus.FAILED
    assert model.invocation_count == 1
    with pytest.raises(ClaimGraphNotFound):
        graph._store.load(example.state.run_id)
