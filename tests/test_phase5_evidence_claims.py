from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FrozenClock
from runtime_fixtures import runtime_example

from researchos.adapters.claim_filesystem import FilesystemClaimGraphStore
from researchos.adapters.claim_memory import InMemoryClaimGraphStore
from researchos.adapters.evidence_filesystem import FilesystemEvidenceStore
from researchos.adapters.evidence_memory import InMemoryEvidenceStore
from researchos.adapters.memory import InMemoryTraceSink
from researchos.adapters.mock_agent import (
    AgentFixture,
    AgentFixtureKey,
    ScriptedAgent,
)
from researchos.application.agent_runner import AgentRunner, AgentRunnerPolicy
from researchos.application.agent_task_backend import AgentTaskExecutionBackend
from researchos.application.capability_registry import CapabilityRegistry
from researchos.application.citation_integrity import CitationIntegrityValidator
from researchos.application.claim_graph import ClaimGraphService
from researchos.application.errors import (
    ClaimGraphPersistenceError,
    ClaimGraphPreconditionError,
    ClaimGraphTraceCommitError,
    CorruptEvidenceStore,
    EvidencePersistenceError,
    EvidenceStoreNotFound,
    EvidenceStoreRevisionConflict,
    EvidenceTraceCommitError,
    UnsafePersistenceData,
)
from researchos.application.evidence_extractor import EvidenceExtractor, sha256_text
from researchos.application.evidence_memory import EvidenceMemory
from researchos.domain.agent import (
    AgentContext,
    AgentDescriptor,
    AgentError,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentFinalDecision,
    AgentFinalResult,
    AgentObservation,
    AgentProducedOutput,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.claims import (
    CitationIssueCode,
    CitationReference,
    ClaimEvidenceRelation,
)
from researchos.domain.contracts import model_sha256
from researchos.domain.evidence import (
    EvidenceIngestionDisposition,
    EvidenceStoreSnapshot,
)
from researchos.domain.runtime import (
    ExecutionResultStatus,
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskExecutionRequest,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    BrowserResult,
    LocalRetrievalHit,
    LocalRetrievalResult,
    SearchHit,
    SearchRequest,
    SearchResult,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def context(*, attempt_id: str = "attempt_a", attempt_number: int = 1) -> AgentContext:
    example = runtime_example()
    task = example.dag.tasks[0]
    return AgentContext(
        run_id=example.state.run_id,
        run_revision=example.state.revision,
        dag_id=example.dag.dag_id,
        task=task,
        task_id=task.task_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        task_operation_key="1" * 64,
        task_attempt_key="2" * 64,
        task_idempotency=IdempotencyMode.IDEMPOTENT,
        deadline=NOW + timedelta(minutes=1),
        hard_limits=RuntimeResourceAmount(
            duration_milliseconds=60_000, tokens=100, cost_microunits=100, tool_calls=10
        ),
        expected_outputs=task.expected_outputs,
        authorized_capability_ids=task.required_capability_ids,
    )


def observation(
    output, *, call_id: str = "call_a", operation_key: str = "3" * 64
) -> AgentObservation:
    return AgentObservation(
        agent_step=1,
        tool_call_id=call_id,
        capability_id="search",
        tool_id="search_tool",
        adapter_id="mock_search",
        tool_input_hash="4" * 64,
        tool_operation_key=operation_key,
        result=ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=output,
            usage=ToolUsage(),
            usage_certainty=UsageCertainty.EXACT,
        ),
    )


def browser(content: str = "Evidence body") -> BrowserResult:
    return BrowserResult(
        adapter_id="mock_search",
        final_url="HTTPS://Example.com:443/page?b=2&a=1#fragment",
        title="Title",
        content=content,
        content_hash=sha256_text(content),
        retrieved_at=NOW,
    )


def build_memory(*, trace=None):
    evidence_store = InMemoryEvidenceStore()
    trace = trace or InMemoryTraceSink()
    memory = EvidenceMemory(
        store=evidence_store, clock=FrozenClock(NOW), trace_sink=trace
    )
    return memory, evidence_store, trace


def test_browser_ingestion_and_runtime_occurrence_replay() -> None:
    memory, store, _ = build_memory()
    first_context = context()
    first = asyncio.run(
        memory.ingest_observation(first_context, observation(browser()))
    )
    retry_context = context(attempt_id="attempt_b", attempt_number=2)
    second = asyncio.run(
        memory.ingest_observation(
            retry_context, observation(browser(), call_id="call_b")
        )
    )

    snapshot = store.load(first_context.run_id)
    assert first.ingestion_operation_key == second.ingestion_operation_key
    assert first.receipt_id != second.receipt_id
    assert len(snapshot.evidence) == len(snapshot.revisions) == 1
    assert len(snapshot.receipts) == 2
    assert len({item.canonical_request_hash for item in snapshot.receipts}) == 1
    assert second.items[0].disposition is EvidenceIngestionDisposition.DEDUPLICATED
    assert snapshot.evidence[0].evidence_scope_key == "browser-page-body-v1"


def test_changed_content_under_same_tool_operation_creates_revision() -> None:
    memory, store, _ = build_memory()
    first_context = context()
    first = asyncio.run(
        memory.ingest_observation(first_context, observation(browser("one")))
    )
    second = asyncio.run(
        memory.ingest_observation(
            context(attempt_id="attempt_b", attempt_number=2),
            observation(browser("two"), call_id="call_b"),
        )
    )
    snapshot = store.load(first_context.run_id)
    assert first.ingestion_operation_key != second.ingestion_operation_key
    assert len(snapshot.evidence) == 1
    assert snapshot.evidence[0].current_revision == 2
    assert len(snapshot.revisions) == 2
    assert len({item.canonical_request_hash for item in snapshot.receipts}) == 2
    assert second.items[0].disposition is EvidenceIngestionDisposition.REVISED


def test_same_content_across_sources_is_classified_duplicate_not_merged() -> None:
    memory, store, _ = build_memory()
    ctx = context()
    first = asyncio.run(memory.ingest_observation(ctx, observation(browser("same"))))
    other = BrowserResult(
        adapter_id="mock_search",
        final_url="https://example.com/other",
        title="Other",
        content="same",
        content_hash=sha256_text("same"),
        retrieved_at=NOW,
    )
    second = asyncio.run(
        memory.ingest_observation(ctx, observation(other, operation_key="5" * 64))
    )
    assert len(store.load(ctx.run_id).evidence) == 2
    assert memory.duplicate_evidence(ctx.run_id, first.items[0].evidence_id) == (
        second.items[0].evidence_id,
    )


def test_exact_occurrence_replay_does_not_mutate_state_and_is_distinct_trace() -> None:
    memory, store, trace = build_memory()
    ctx = context()
    item = observation(browser())
    first = asyncio.run(memory.ingest_observation(ctx, item))
    replay = asyncio.run(memory.ingest_observation(ctx, item))
    assert replay.replayed is True
    assert replay.store_revision == first.store_revision
    assert store.load(ctx.run_id).store_revision == first.store_revision
    assert trace.read(ctx.run_id)[-1].event_type.value == "evidence.replayed"


def test_extractor_specific_search_and_local_scopes_are_stable() -> None:
    extractor = EvidenceExtractor()
    ctx = context()
    search = SearchResult(
        adapter_id="mock_search",
        hits=(
            SearchHit(
                locator="native-snippet",
                url="https://EXAMPLE.com/result#x",
                title="T",
                snippet="snippet",
                retrieved_at=NOW,
                adapter_id="mock_search",
            ),
        ),
    )
    one = extractor.extract(ctx, observation(search))[0]
    two = extractor.extract(ctx, observation(search))[0]
    assert one.evidence_scope_key == two.evidence_scope_key
    assert "native-snippet" not in one.evidence_scope_key

    content = "local content"
    local = LocalRetrievalResult(
        adapter_id="local",
        retrieved_at=NOW,
        matched_count=1,
        hits=(
            LocalRetrievalHit(
                document_id="doc_a",
                chunk_id="chunk_a",
                locator="docs/a.txt",
                content=content,
                content_hash=sha256_text(content),
                score=1.0,
            ),
        ),
    )
    candidate = extractor.extract(ctx, observation(local))[0]
    assert candidate.canonical_locator == "local://doc_a"
    assert candidate.evidence_scope_key.startswith("scope_")


def test_failed_tool_result_is_not_eligible() -> None:
    failed = AgentObservation(
        agent_step=1,
        tool_call_id="call_a",
        capability_id="search",
        tool_id="search_tool",
        adapter_id="mock_search",
        tool_input_hash="4" * 64,
        tool_operation_key="3" * 64,
        result=ToolInvocationResult(
            status=ToolInvocationStatus.FAILED,
            error={"code": "failed", "message": "failed"},
            usage=ToolUsage(),
            usage_certainty=UsageCertainty.EXACT,
        ),
    )
    assert EvidenceExtractor().extract(context(), failed) == ()


def test_malformed_content_hash_and_secret_content_are_not_persisted() -> None:
    bad_hash = browser("content").model_copy(update={"content_hash": "0" * 64})
    assert EvidenceExtractor().extract(context(), observation(bad_hash)) == ()
    memory, store, _ = build_memory()
    with pytest.raises(UnsafePersistenceData):
        asyncio.run(
            memory.ingest_observation(
                context(), observation(browser("Bearer abcdefghijklmnop"))
            )
        )
    with pytest.raises(EvidenceStoreNotFound):
        store.load(context().run_id)


class FailingTrace:
    def append(self, event) -> None:
        del event
        raise OSError("fault")

    def read(self, run_id, *, recover_torn_tail=False):
        del run_id, recover_torn_tail
        return ()


class ToggleTrace(InMemoryTraceSink):
    fail = True

    def append(self, event) -> None:
        if self.fail:
            raise OSError("fault")
        super().append(event)


def test_trace_failure_reports_already_committed_store() -> None:
    memory, store, _ = build_memory(trace=FailingTrace())
    ctx = context()
    with pytest.raises(EvidenceTraceCommitError) as caught:
        asyncio.run(memory.ingest_observation(ctx, observation(browser())))
    assert caught.value.store_committed is True
    assert store.load(ctx.run_id).store_revision == 1


def test_filesystem_evidence_snapshot_roundtrip_and_corruption(tmp_path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    snapshot = EvidenceStoreSnapshot(run_id="run_a", store_revision=0)
    store.create(snapshot)
    first_bytes = (tmp_path / "run_a" / "evidence.jsonl").read_bytes()
    assert store.load("run_a") == snapshot
    assert first_bytes.endswith(b"\n")
    path = tmp_path / "run_a" / "evidence.jsonl"
    path.write_bytes(first_bytes.replace(b'"record_count":0', b'"record_count":1'))
    with pytest.raises(CorruptEvidenceStore):
        store.load("run_a")


def test_filesystem_failure_exposes_replace_state(tmp_path) -> None:
    def fault(point: str) -> None:
        if point == "after_replace":
            raise OSError("fault")

    store = FilesystemEvidenceStore(tmp_path, fault_injector=fault)
    with pytest.raises(EvidencePersistenceError) as caught:
        store.create(EvidenceStoreSnapshot(run_id="run_a", store_revision=0))
    assert caught.value.store_replaced is True


def test_filesystem_evidence_compare_and_swap(tmp_path) -> None:
    store = FilesystemEvidenceStore(tmp_path)
    snapshot = EvidenceStoreSnapshot(run_id="run_a", store_revision=0)
    store.create(snapshot)
    with pytest.raises(EvidenceStoreRevisionConflict):
        store.save(
            snapshot.model_copy(update={"store_revision": 1}), expected_revision=9
        )


def graph_services():
    memory, evidence_store, trace = build_memory()
    graph_store = InMemoryClaimGraphStore()
    graph = ClaimGraphService(
        store=graph_store,
        evidence_store=evidence_store,
        clock=FrozenClock(NOW),
        trace_sink=trace,
    )
    return memory, evidence_store, graph_store, graph


def test_claim_identity_is_scope_stable_and_statement_is_revisioned() -> None:
    _, _, _, graph = graph_services()
    ctx = context()
    created = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="Initial claim",
    )
    revised = graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=created.claim.claim_id,
        statement="Changed claim",
        expected_revision=1,
    )
    other = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_b",
        statement="Changed claim",
    )
    assert revised.claim.claim_id == created.claim.claim_id
    assert revised.revision.revision == 2
    assert other.claim.claim_id != revised.claim.claim_id
    assert (
        other.revision.normalized_statement_hash
        == revised.revision.normalized_statement_hash
    )
    assert graph.duplicate_claims(ctx.run_id, revised.claim.claim_id) == (
        other.claim.claim_id,
    )


def test_claim_revision_receipt_replays_after_terminal_trace_failure() -> None:
    evidence_store = InMemoryEvidenceStore()
    claim_store = InMemoryClaimGraphStore()
    trace = ToggleTrace()
    graph = ClaimGraphService(
        store=claim_store,
        evidence_store=evidence_store,
        clock=FrozenClock(NOW),
        trace_sink=trace,
    )
    ctx = context()
    with pytest.raises(ClaimGraphTraceCommitError):
        graph.create_claim(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_scope_key="scope_a",
            statement="claim",
        )
    assert claim_store.load(ctx.run_id).store_revision == 1
    trace.fail = False
    replay = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim",
    )
    assert replay.revision.revision == 1
    assert trace.read(ctx.run_id)[-1].event_type.value == "claim.mutation_replayed"


def test_relation_changes_revision_and_conflict_detection() -> None:
    memory, _, _, graph = graph_services()
    ctx = context()
    first = asyncio.run(memory.ingest_observation(ctx, observation(browser("support"))))
    second_output = BrowserResult(
        adapter_id="mock_search",
        final_url="https://example.com/other",
        title="Other",
        content="contradict",
        content_hash=sha256_text("contradict"),
        retrieved_at=NOW,
    )
    second = asyncio.run(
        memory.ingest_observation(
            ctx, observation(second_output, operation_key="5" * 64)
        )
    )
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="A claim",
    )
    edge = graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=first.items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTEXTUALIZES,
    )
    changed = graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=first.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=second.items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTRADICTS,
    )
    assert edge.edge.edge_id == changed.edge.edge_id
    assert changed.revision.revision == 2
    assert graph.conflict_candidates(ctx.run_id)[0].claim_id == claim.claim.claim_id
    tombstoned = graph.tombstone_edge(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        edge_id=changed.edge.edge_id,
    )
    assert tombstoned.lifecycle.value == "tombstoned"


def test_relation_rejects_missing_evidence() -> None:
    _, _, _, graph = graph_services()
    ctx = context()
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="A claim",
    )
    with pytest.raises(ClaimGraphPreconditionError):
        graph.relate(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            evidence_id="ev_missing",
            relation=ClaimEvidenceRelation.SUPPORTS,
        )


def test_citation_staleness_is_warning_and_missing_revision_is_error() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    ingested = asyncio.run(memory.ingest_observation(ctx, observation(browser("one"))))
    asyncio.run(
        memory.ingest_observation(
            context(attempt_id="attempt_b", attempt_number=2),
            observation(browser("two"), call_id="call_b"),
        )
    )
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="one",
    )
    graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="two",
        expected_revision=1,
    )
    validator = CitationIntegrityValidator(
        claim_store=claim_store, evidence_store=evidence_store
    )
    stale = validator.validate(
        ctx.run_id,
        (
            CitationReference(
                citation_id="citation_a",
                claim_id=claim.claim.claim_id,
                claim_revision=1,
                evidence_id=ingested.items[0].evidence_id,
                evidence_revision=1,
            ),
        ),
    )
    assert stale.valid is True
    assert {issue.code for issue in stale.issues} == {
        CitationIssueCode.STALE_CLAIM_REVISION,
        CitationIssueCode.STALE_EVIDENCE_REVISION,
    }
    missing = validator.validate(
        ctx.run_id,
        (
            CitationReference(
                citation_id="citation_b",
                claim_id=claim.claim.claim_id,
                claim_revision=99,
                evidence_id=ingested.items[0].evidence_id,
                evidence_revision=99,
            ),
        ),
    )
    assert missing.valid is False
    assert CitationIssueCode.MISSING_REVISION in {
        issue.code for issue in missing.issues
    }


def test_filesystem_claim_snapshot_roundtrip(tmp_path) -> None:
    from researchos.domain.claims import ClaimGraphSnapshot

    store = FilesystemClaimGraphStore(tmp_path)
    snapshot = ClaimGraphSnapshot(run_id="run_a", store_revision=0)
    store.create(snapshot)
    assert store.load("run_a") == snapshot


def test_filesystem_claim_failure_exposes_replace_state(tmp_path) -> None:
    from researchos.domain.claims import ClaimGraphSnapshot

    def fault(point: str) -> None:
        if point == "after_replace":
            raise OSError("fault")

    store = FilesystemClaimGraphStore(tmp_path, fault_injector=fault)
    with pytest.raises(ClaimGraphPersistenceError) as caught:
        store.create(ClaimGraphSnapshot(run_id="run_a", store_revision=0))
    assert caught.value.store_replaced is True


def test_evidence_query_is_bounded_and_trace_has_no_raw_payload() -> None:
    memory, _, trace = build_memory()
    ctx = context()
    asyncio.run(memory.ingest_observation(ctx, observation(browser("private fact"))))
    rows = memory.list_evidence(ctx.run_id, limit=1)
    assert len(rows) == 1
    assert "private fact" not in str(trace.read(ctx.run_id)[-1].attributes)
    with pytest.raises(ValueError):
        memory.list_evidence(ctx.run_id, limit=101)


class StaticRunner:
    def __init__(self, result) -> None:
        self.result = result

    async def run(self, context, cancellation):
        del context, cancellation
        return self.result


class RecordingIngestor:
    def __init__(self, *, fail: bool = False) -> None:
        self.observations = []
        self.fail = fail

    async def ingest_observation(self, context, item):
        del context
        self.observations.append(item)
        if self.fail:
            raise OSError("ingestion failed")
        return None


class NeverCancelled:
    cancelled = False

    async def wait(self):
        await asyncio.Future()


def execution_request() -> TaskExecutionRequest:
    example = runtime_example()
    task = example.dag.tasks[0]
    policy = next(item for item in example.policy.tasks if item.task_id == task.task_id)
    return TaskExecutionRequest(
        run_id=example.state.run_id,
        run_revision=example.state.revision,
        dag_id=example.dag.dag_id,
        dag_hash=model_sha256(example.dag),
        task=task,
        task_id=task.task_id,
        attempt_id="attempt_a",
        attempt_number=1,
        operation_key="1" * 64,
        attempt_key="2" * 64,
        operation_version=policy.operation_version,
        task_idempotency=policy.idempotency,
        deadline=NOW + timedelta(minutes=1),
        hard_limits=policy.reservation,
    )


def test_backend_ingests_successful_observation_before_mapping_agent_failure() -> None:
    item = observation(browser())
    agent_result = AgentExecutionResult(
        status=AgentExecutionStatus.FAILED,
        error=AgentError(code="agent_failed", message="failed"),
        usage=RuntimeResourceAmount(tokens=1),
        usage_certainty=UsageCertainty.EXACT,
        observations=(item,),
    )
    ingestor = RecordingIngestor()
    backend = AgentTaskExecutionBackend(
        StaticRunner(agent_result), evidence_ingestor=ingestor
    )
    result = asyncio.run(backend.execute(execution_request(), NeverCancelled()))
    assert ingestor.observations == [item]
    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "agent_failed"


def test_backend_reports_ingestion_failure_without_hiding_usage() -> None:
    request = execution_request()
    output = request.task.expected_outputs[0]
    agent_result = AgentExecutionResult(
        status=AgentExecutionStatus.SUCCEEDED,
        final=AgentFinalResult(
            outputs=(
                AgentProducedOutput(
                    output_id=output.output_id, media_type=output.media_type
                ),
            )
        ),
        usage=RuntimeResourceAmount(tokens=3),
        usage_certainty=UsageCertainty.EXACT,
        observations=(observation(browser()),),
    )
    backend = AgentTaskExecutionBackend(
        StaticRunner(agent_result), evidence_ingestor=RecordingIngestor(fail=True)
    )
    result = asyncio.run(backend.execute(request, NeverCancelled()))
    assert result.failure_code == "evidence_ingestion_failed"
    assert result.usage == RuntimeResourceAmount(tokens=3)


class NeverSleeper:
    async def sleep(self, milliseconds: int) -> None:
        del milliseconds
        await asyncio.Future()


class SearchTool:
    descriptor = ToolDescriptor(
        tool_id="search_tool",
        capability_id="search",
        adapter_id="mock_search",
        mode=AdapterMode.MOCK,
        operation_version="v1",
        input_type="search",
        output_type="search_result",
        side_effect=ToolSideEffect.NONE,
        idempotency=IdempotencyMode.IDEMPOTENT,
    )

    async def invoke(self, request, cancellation):
        del request, cancellation
        return observation(
            SearchResult(
                adapter_id="mock_search",
                hits=(
                    SearchHit(
                        locator="native_a",
                        url="https://example.com/a",
                        title="A",
                        snippet="retrieved fact",
                        retrieved_at=NOW,
                        adapter_id="mock_search",
                    ),
                ),
            )
        ).result


def test_runner_backend_tool_to_evidence_retry_integration() -> None:
    request = execution_request()
    output = request.task.expected_outputs[0]
    fixtures = {}
    for attempt in (1, 2):
        fixtures[AgentFixtureKey(request.task_id, attempt, 1)] = AgentFixture(
            decision=AgentToolDecision(
                tool_call=AgentToolCall(
                    tool_call_id=f"call_{attempt}",
                    capability_id="search",
                    input=SearchRequest(query="stable query"),
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
        fixtures[AgentFixtureKey(request.task_id, attempt, 2)] = AgentFixture(
            decision=AgentFinalDecision(
                final=AgentFinalResult(
                    outputs=(
                        AgentProducedOutput(
                            output_id=output.output_id,
                            media_type=output.media_type,
                        ),
                    )
                ),
                usage=RuntimeResourceAmount(),
                usage_certainty=UsageCertainty.EXACT,
            )
        )
    agent = ScriptedAgent(
        AgentDescriptor(
            agent_id="phase5_agent",
            adapter_id="scripted",
            mode=AdapterMode.MOCK,
            operation_version="v1",
        ),
        fixtures,
    )
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(SearchTool())
    trace = InMemoryTraceSink()
    runner = AgentRunner(
        agent=agent,
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=2, max_tool_calls=1),
        clock=FrozenClock(NOW),
        sleeper=NeverSleeper(),
        trace_sink=trace,
    )
    store = InMemoryEvidenceStore()
    memory = EvidenceMemory(store=store, clock=FrozenClock(NOW), trace_sink=trace)
    backend = AgentTaskExecutionBackend(runner, evidence_ingestor=memory)

    first = asyncio.run(backend.execute(request, NeverCancelled()))
    retry = request.model_copy(
        update={
            "attempt_id": "attempt_b",
            "attempt_number": 2,
            "attempt_key": "9" * 64,
        }
    )
    second = asyncio.run(backend.execute(retry, NeverCancelled()))

    snapshot = store.load(request.run_id)
    assert first.status is second.status is ExecutionResultStatus.SUCCEEDED
    assert len(snapshot.evidence) == len(snapshot.revisions) == 1
    assert len(snapshot.receipts) == 2
