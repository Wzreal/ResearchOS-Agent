from __future__ import annotations

import asyncio

import pytest
from conftest import FrozenClock
from pydantic import ValidationError
from test_phase4_agent_tools import NeverSleeper
from test_phase5_evidence_claims import (
    NOW,
    NeverCancelled,
    StaticRunner,
    ToggleTrace,
    browser,
    build_memory,
    context,
    execution_request,
    graph_services,
    observation,
)

from researchos.adapters.claim_memory import InMemoryClaimGraphStore
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
    ClaimGraphPreconditionError,
    EvidenceStoreNotFound,
    EvidenceTraceCommitError,
)
from researchos.application.evidence_extractor import EvidenceExtractor, sha256_text
from researchos.domain.agent import (
    AgentDescriptor,
    AgentError,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentFailedDecision,
    AgentObservation,
    AgentToolCall,
    AgentToolDecision,
)
from researchos.domain.claims import (
    CitationIssueCode,
    CitationReference,
    ClaimEvidenceRelation,
    ClaimGenerationContext,
)
from researchos.domain.evidence import RecordLifecycle
from researchos.domain.runtime import (
    ExecutionResultStatus,
    IdempotencyMode,
    RuntimeResourceAmount,
    UsageCertainty,
)
from researchos.domain.tools import (
    AdapterMode,
    BrowserResult,
    PythonExecutionResult,
    SearchHit,
    SearchRequest,
    SearchResult,
    ToolArtifact,
    ToolDescriptor,
    ToolInvocationResult,
    ToolInvocationStatus,
    ToolSideEffect,
    ToolUsage,
)


def _browser_at(url: str, content: str) -> BrowserResult:
    return BrowserResult(
        adapter_id="mock_search",
        final_url=url,
        title="Title",
        content=content,
        content_hash=sha256_text(content),
        retrieved_at=NOW,
    )


def _ingest(memory, ctx, url: str, content: str, operation_key: str):
    return asyncio.run(
        memory.ingest_observation(
            ctx,
            observation(_browser_at(url, content), operation_key=operation_key),
        )
    )


def test_claim_generation_context_revisions_do_not_change_logical_identity() -> None:
    _, _, _, graph = graph_services()
    ctx = context()
    first_context = ClaimGenerationContext(
        run_revision=ctx.run_revision,
        producer_id="agent_a",
        task_id=ctx.task_id,
        source_operation_key="1" * 64,
    )
    second_context = first_context.model_copy(update={"producer_id": "agent_b"})
    created = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="stable statement",
        generation_context=first_context,
    )
    revised = graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=created.claim.claim_id,
        statement="stable statement",
        expected_revision=1,
        generation_context=second_context,
    )
    assert revised.claim.claim_id == created.claim.claim_id
    assert revised.revision.revision == 2
    assert revised.revision.generation_context == second_context
    with pytest.raises(ValidationError):
        created.claim.claim_scope_key = "scope_b"  # type: ignore[misc]


def test_normalized_duplicate_across_sources_does_not_merge_raw_content() -> None:
    memory, store, _ = build_memory()
    ctx = context()
    first = _ingest(memory, ctx, "https://example.com/a", "alpha  beta", "5" * 64)
    second = _ingest(memory, ctx, "https://example.com/b", "alpha beta", "6" * 64)
    snapshot = store.load(ctx.run_id)
    first_revision = next(
        item
        for item in snapshot.revisions
        if item.evidence_id == first.items[0].evidence_id
    )
    second_revision = next(
        item
        for item in snapshot.revisions
        if item.evidence_id == second.items[0].evidence_id
    )
    assert first_revision.content_hash != second_revision.content_hash
    assert (
        first_revision.normalized_content_hash
        == second_revision.normalized_content_hash
    )
    assert len(snapshot.evidence) == 2
    assert memory.duplicate_evidence(ctx.run_id, first.items[0].evidence_id) == (
        second.items[0].evidence_id,
    )


def test_search_scope_uses_query_context_but_not_rank_or_runtime_occurrence() -> None:
    extractor = EvidenceExtractor()
    ctx = context()
    hit = SearchHit(
        locator="rank_1",
        url="https://example.com/item",
        title="Title",
        snippet="same snippet",
        retrieved_at=NOW,
        adapter_id="mock_search",
    )
    output = SearchResult(adapter_id="mock_search", hits=(hit,))
    base = observation(output)
    same_query_other_rank = base.model_copy(
        update={
            "tool_call_id": "call_b",
            "result": base.result.model_copy(
                update={
                    "output": output.model_copy(
                        update={
                            "hits": (hit.model_copy(update={"locator": "rank_99"}),)
                        }
                    )
                }
            ),
        }
    )
    retry_context = ctx.model_copy(
        update={"attempt_id": "attempt_b", "attempt_number": 2}
    )
    different_query = base.model_copy(update={"tool_input_hash": "9" * 64})

    first_scope = extractor.extract(ctx, base)[0].evidence_scope_key
    retry_scope = extractor.extract(retry_context, same_query_other_rank)[
        0
    ].evidence_scope_key
    other_scope = extractor.extract(ctx, different_query)[0].evidence_scope_key
    assert retry_scope == first_scope
    assert other_scope != first_scope


def _terminal_observation(
    status: ToolInvocationStatus, call_id: str
) -> AgentObservation:
    return AgentObservation(
        agent_step=3,
        tool_call_id=call_id,
        capability_id="search",
        tool_id="search_tool",
        adapter_id="mock_search",
        tool_input_hash="7" * 64,
        tool_operation_key="8" * 64,
        result=ToolInvocationResult(
            status=status,
            error={"code": "terminal", "message": "terminal"},
            usage=ToolUsage(),
            usage_certainty=UsageCertainty.EXACT,
        ),
    )


def test_failed_agent_ingests_two_prior_successes_but_no_ineligible_results() -> None:
    request = execution_request()
    ctx = context()
    successes = (
        observation(
            _browser_at("https://example.com/a", "one"), operation_key="3" * 64
        ),
        observation(
            _browser_at("https://example.com/b", "two"), operation_key="4" * 64
        ),
    )
    python_observation = observation(
        PythonExecutionResult(exit_code=0, stdout="not evidence", stderr=""),
        operation_key="5" * 64,
    )
    artifact = ToolArtifact(
        artifact_id="artifact_a",
        media_type="text/plain",
        relative_path="artifact.txt",
        sha256="0" * 64,
        size_bytes=0,
        producer_tool_id="search_tool",
        tool_operation_key="3" * 64,
    )
    successes_with_artifact = successes[0].model_copy(
        update={
            "result": successes[0].result.model_copy(update={"artifacts": (artifact,)})
        }
    )
    observations = (
        successes_with_artifact,
        successes[1],
        _terminal_observation(ToolInvocationStatus.FAILED, "call_failed"),
        _terminal_observation(ToolInvocationStatus.CANCELLED, "call_cancelled"),
        _terminal_observation(ToolInvocationStatus.TIMED_OUT, "call_timed"),
        python_observation,
    )
    terminal = AgentExecutionResult(
        status=AgentExecutionStatus.FAILED,
        error=AgentError(code="agent_failed", message="agent failed"),
        usage=RuntimeResourceAmount(tokens=5),
        usage_certainty=UsageCertainty.EXACT,
        observations=observations,
    )
    memory, store, _ = build_memory()
    backend = AgentTaskExecutionBackend(
        StaticRunner(terminal), evidence_ingestor=memory
    )
    result = asyncio.run(backend.execute(request, NeverCancelled()))
    snapshot = store.load(ctx.run_id)
    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "agent_failed"
    assert len(snapshot.evidence) == 2
    assert {item.content for item in snapshot.revisions} == {"one", "two"}


class QuerySearchTool:
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

    def __init__(self, *, tokens: int = 0) -> None:
        self._tokens = tokens

    async def invoke(self, request, cancellation):
        del cancellation
        query = request.input.query
        return ToolInvocationResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=SearchResult(
                adapter_id="mock_search",
                hits=(
                    SearchHit(
                        locator=f"native_{query}",
                        url=f"https://example.com/{query}",
                        title=query,
                        snippet=f"evidence {query}",
                        retrieved_at=NOW,
                        adapter_id="mock_search",
                    ),
                ),
            ),
            usage=ToolUsage(tokens=self._tokens, tool_calls=1),
            usage_certainty=UsageCertainty.EXACT,
        )


def test_real_runner_failed_after_two_tools_still_ingests_both_observations() -> None:
    request = execution_request()
    fixtures = {
        AgentFixtureKey(request.task_id, 1, step): AgentFixture(decision=decision)
        for step, decision in (
            (
                1,
                AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="one"),
                    ),
                    usage=RuntimeResourceAmount(),
                    usage_certainty=UsageCertainty.EXACT,
                ),
            ),
            (
                2,
                AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_two",
                        capability_id="search",
                        input=SearchRequest(query="two"),
                    ),
                    usage=RuntimeResourceAmount(),
                    usage_certainty=UsageCertainty.EXACT,
                ),
            ),
            (
                3,
                AgentFailedDecision(
                    error=AgentError(code="late_agent_failure", message="failed"),
                    usage=RuntimeResourceAmount(),
                    usage_certainty=UsageCertainty.EXACT,
                ),
            ),
        )
    }
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
    registry.register(QuerySearchTool())
    trace = InMemoryTraceSink()
    runner = AgentRunner(
        agent=agent,
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=3, max_tool_calls=2),
        clock=FrozenClock(NOW),
        sleeper=NeverSleeper(),
        trace_sink=trace,
    )
    memory, store, _ = build_memory(trace=trace)
    result = asyncio.run(
        AgentTaskExecutionBackend(runner, evidence_ingestor=memory).execute(
            request, NeverCancelled()
        )
    )

    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "late_agent_failure"
    snapshot = store.load(request.run_id)
    assert {item.content for item in snapshot.revisions} == {
        "evidence one",
        "evidence two",
    }


def test_successful_tool_observation_survives_later_hard_limit_failure() -> None:
    request = execution_request().model_copy(
        update={"hard_limits": RuntimeResourceAmount(tokens=0, tool_calls=1)}
    )
    agent = ScriptedAgent(
        AgentDescriptor(
            agent_id="phase5_agent",
            adapter_id="scripted",
            mode=AdapterMode.MOCK,
            operation_version="v1",
        ),
        {
            AgentFixtureKey(request.task_id, 1, 1): AgentFixture(
                decision=AgentToolDecision(
                    tool_call=AgentToolCall(
                        tool_call_id="call_one",
                        capability_id="search",
                        input=SearchRequest(query="one"),
                    ),
                    usage=RuntimeResourceAmount(),
                    usage_certainty=UsageCertainty.EXACT,
                )
            )
        },
    )
    registry = CapabilityRegistry(allowed_modes=frozenset({AdapterMode.MOCK}))
    registry.register(QuerySearchTool(tokens=1))
    trace = InMemoryTraceSink()
    runner = AgentRunner(
        agent=agent,
        registry=registry,
        policy=AgentRunnerPolicy(max_agent_steps=1, max_tool_calls=1),
        clock=FrozenClock(NOW),
        sleeper=NeverSleeper(),
        trace_sink=trace,
    )
    memory, store, _ = build_memory(trace=trace)
    result = asyncio.run(
        AgentTaskExecutionBackend(runner, evidence_ingestor=memory).execute(
            request, NeverCancelled()
        )
    )

    assert result.status is ExecutionResultStatus.FAILED
    assert result.failure_code == "agent_hard_limit_exceeded"
    snapshot = store.load(request.run_id)
    assert [item.content for item in snapshot.revisions] == ["evidence one"]


def test_edge_relation_revisions_keep_one_current_lineage() -> None:
    memory, _, graph_store, graph = graph_services()
    ctx = context()
    evidence = _ingest(memory, ctx, "https://example.com/a", "one", "5" * 64)
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim",
    )
    supports = graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    contradicts = graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence.items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTRADICTS,
    )
    snapshot = graph_store.load(ctx.run_id)
    assert supports.edge.edge_id == contradicts.edge.edge_id
    assert contradicts.revision.revision == 2
    assert [item.relation for item in snapshot.edge_revisions] == [
        ClaimEvidenceRelation.SUPPORTS,
        ClaimEvidenceRelation.CONTRADICTS,
    ]
    assert len(snapshot.edges) == 1
    assert graph.contradicting_evidence(ctx.run_id, claim.claim.claim_id) == (
        contradicts,
    )
    assert graph.supporting_evidence(ctx.run_id, claim.claim.claim_id) == ()
    assert (
        graph.get_edge(ctx.run_id, supports.edge.edge_id, revision=1).revision.relation
        is ClaimEvidenceRelation.SUPPORTS
    )


@pytest.mark.parametrize(
    "relation",
    [
        ClaimEvidenceRelation.SUPPORTS,
        ClaimEvidenceRelation.CONTRADICTS,
        ClaimEvidenceRelation.CONTEXTUALIZES,
    ],
)
def test_single_relation_never_forms_conflict_candidate(relation) -> None:
    memory, _, _, graph = graph_services()
    ctx = context()
    evidence = _ingest(memory, ctx, "https://example.com/a", "one", "5" * 64)
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence.items[0].evidence_id,
        relation=relation,
    )
    assert graph.conflict_candidates(ctx.run_id) == ()


def test_conflict_candidates_require_two_distinct_current_valid_edges() -> None:
    memory, _, _, graph = graph_services()
    ctx = context()
    evidence = [
        _ingest(
            memory,
            ctx,
            f"https://example.com/{index}",
            f"content {index}",
            str(index) * 64,
        )
        for index in range(1, 5)
    ]
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence[0].items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    assert graph.conflict_candidates(ctx.run_id) == ()
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence[1].items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTRADICTS,
    )
    assert len(graph.conflict_candidates(ctx.run_id)) == 1
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=evidence[2].items[0].evidence_id,
        relation=ClaimEvidenceRelation.CONTEXTUALIZES,
    )
    memory.tombstone(
        ctx.run_id,
        evidence[1].items[0].evidence_id,
        run_revision=ctx.run_revision,
    )
    assert graph.conflict_candidates(ctx.run_id) == ()


def test_stale_pinned_claim_or_evidence_revision_is_not_current_conflict() -> None:
    memory, _, _, graph = graph_services()
    ctx = context()
    support = _ingest(memory, ctx, "https://example.com/a", "support", "5" * 64)
    contradict = _ingest(memory, ctx, "https://example.com/b", "old", "6" * 64)
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim v1",
    )
    for item, relation in (
        (support, ClaimEvidenceRelation.SUPPORTS),
        (contradict, ClaimEvidenceRelation.CONTRADICTS),
    ):
        graph.relate(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            evidence_id=item.items[0].evidence_id,
            relation=relation,
        )
    assert len(graph.conflict_candidates(ctx.run_id)) == 1
    graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="claim v2",
        expected_revision=1,
    )
    assert graph.conflict_candidates(ctx.run_id) == ()

    fresh_claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_b",
        statement="other claim",
    )
    for item, relation in (
        (support, ClaimEvidenceRelation.SUPPORTS),
        (contradict, ClaimEvidenceRelation.CONTRADICTS),
    ):
        graph.relate(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=fresh_claim.claim.claim_id,
            evidence_id=item.items[0].evidence_id,
            relation=relation,
        )
    _ingest(memory, ctx, "https://example.com/b", "new", "6" * 64)
    assert graph.conflict_candidates(ctx.run_id) == ()


def test_graph_queries_are_ordered_bounded_reverse_and_historical() -> None:
    memory, _, _, graph = graph_services()
    ctx = context()
    evidence = [
        _ingest(
            memory, ctx, f"https://example.com/{index}", str(index), str(index) * 64
        )
        for index in range(1, 4)
    ]
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="v1",
    )
    revised = graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="v2",
        expected_revision=1,
    )
    edges = [
        graph.relate(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            evidence_id=item.items[0].evidence_id,
            relation=relation,
        )
        for item, relation in zip(
            evidence,
            (
                ClaimEvidenceRelation.SUPPORTS,
                ClaimEvidenceRelation.CONTRADICTS,
                ClaimEvidenceRelation.CONTEXTUALIZES,
            ),
            strict=True,
        )
    ]
    assert (
        graph.get_claim(ctx.run_id, claim.claim.claim_id, revision=1).revision.statement
        == "v1"
    )
    assert (
        graph.get_claim(ctx.run_id, claim.claim.claim_id).revision == revised.revision
    )
    ordered = graph.evidence_for_claim(ctx.run_id, claim.claim.claim_id, limit=2)
    assert [item.edge.edge_id for item in ordered] == sorted(
        item.edge.edge_id for item in edges
    )[:2]
    page = graph.evidence_for_claim(
        ctx.run_id,
        claim.claim.claim_id,
        after_id=ordered[-1].edge.edge_id,
        limit=2,
    )
    assert len(page) == 1
    assert graph.claims_for_evidence(ctx.run_id, edges[0].edge.evidence_id) == (
        edges[0],
    )
    assert graph.supporting_evidence(ctx.run_id, claim.claim.claim_id) == (edges[0],)
    assert graph.contradicting_evidence(ctx.run_id, claim.claim.claim_id) == (edges[1],)
    assert graph.contextualizing_evidence(ctx.run_id, claim.claim.claim_id) == (
        edges[2],
    )
    assert (
        graph.get_edge(ctx.run_id, edges[0].edge.edge_id).revision == edges[0].revision
    )
    with pytest.raises(ValueError):
        graph.evidence_for_claim(ctx.run_id, claim.claim.claim_id, limit=101)

    first_evidence = evidence[0].items[0].evidence_id
    _ingest(memory, ctx, "https://example.com/1", "changed", "1" * 64)
    assert (
        memory.get_evidence(ctx.run_id, first_evidence, revision=1).revision.content
        == "1"
    )
    memory.tombstone(ctx.run_id, first_evidence, run_revision=ctx.run_revision)
    with pytest.raises(EvidenceStoreNotFound):
        memory.get_evidence(ctx.run_id, first_evidence)
    assert (
        memory.get_evidence(
            ctx.run_id, first_evidence, include_tombstoned=True
        ).evidence.lifecycle
        is RecordLifecycle.TOMBSTONED
    )
    graph.tombstone_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
    )
    with pytest.raises(ClaimGraphPreconditionError):
        graph.get_claim(ctx.run_id, claim.claim.claim_id)
    assert (
        graph.get_claim(
            ctx.run_id, claim.claim.claim_id, include_tombstoned=True
        ).claim.lifecycle
        is RecordLifecycle.TOMBSTONED
    )


def test_citation_warnings_remain_valid_and_duplicate_id_conflict_is_error() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    first = _ingest(memory, ctx, "https://example.com/a", "v1", "5" * 64)
    second = _ingest(memory, ctx, "https://example.com/b", "other", "6" * 64)
    _ingest(memory, ctx, "https://example.com/a", "v2", "5" * 64)
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="v1",
    )
    graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="v2",
        expected_revision=1,
    )
    graph.tombstone_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
    )
    memory.tombstone(
        ctx.run_id,
        first.items[0].evidence_id,
        run_revision=ctx.run_revision,
    )
    validator = CitationIntegrityValidator(
        claim_store=claim_store, evidence_store=evidence_store
    )
    historical = CitationReference(
        citation_id="citation_a",
        claim_id=claim.claim.claim_id,
        claim_revision=1,
        evidence_id=first.items[0].evidence_id,
        evidence_revision=1,
    )
    warnings = validator.validate(ctx.run_id, (historical,))
    assert warnings.valid is True
    assert warnings.has_warnings is True
    assert {
        CitationIssueCode.STALE_CLAIM_REVISION,
        CitationIssueCode.STALE_EVIDENCE_REVISION,
        CitationIssueCode.TOMBSTONED_CLAIM,
        CitationIssueCode.TOMBSTONED_EVIDENCE,
    } <= {item.code for item in warnings.issues}

    conflicting = historical.model_copy(
        update={
            "evidence_id": second.items[0].evidence_id,
            "evidence_revision": 1,
        }
    )
    errors = validator.validate(ctx.run_id, (historical, conflicting))
    assert errors.valid is False
    assert CitationIssueCode.CONFLICTING_DUPLICATE_CITATION_ID in {
        item.code for item in errors.issues
    }


def test_evidence_trace_failure_retry_replays_receipt_without_new_revision() -> None:
    trace = ToggleTrace()
    memory, store, _ = build_memory(trace=trace)
    ctx = context()
    item = observation(browser("authoritative evidence"))
    with pytest.raises(EvidenceTraceCommitError) as caught:
        asyncio.run(memory.ingest_observation(ctx, item))
    committed = store.load(ctx.run_id)
    assert caught.value.store_committed is True
    assert len(committed.evidence) == len(committed.revisions) == 1

    trace.fail = False
    replay = asyncio.run(memory.ingest_observation(ctx, item))
    after = store.load(ctx.run_id)
    assert replay.replayed is True
    assert after.store_revision == committed.store_revision
    assert len(after.evidence) == len(after.revisions) == 1
    assert trace.read(ctx.run_id)[-1].event_type.value == "evidence.replayed"


def test_claim_entity_revision_compare_and_swap_rejects_stale_writer() -> None:
    _, _, _, graph = graph_services()
    ctx = context()
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="v1",
    )
    graph.revise_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        statement="v2",
        expected_revision=1,
    )
    with pytest.raises(ClaimGraphPreconditionError):
        graph.revise_claim(
            run_id=ctx.run_id,
            run_revision=ctx.run_revision,
            claim_id=claim.claim.claim_id,
            statement="stale writer",
            expected_revision=1,
        )


def test_phase5_trace_attributes_exclude_raw_claim_evidence_and_source_text() -> None:
    memory, evidence_store, trace = build_memory()
    ctx = context()
    ingested = _ingest(
        memory,
        ctx,
        "https://example.com/sensitive-path",
        "private source body",
        "5" * 64,
    )
    graph = ClaimGraphService(
        store=InMemoryClaimGraphStore(),
        evidence_store=evidence_store,
        clock=FrozenClock(NOW),
        trace_sink=trace,
    )
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="private claim statement",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=ingested.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    attributes = str([item.attributes for item in trace.read(ctx.run_id)])
    assert "private source body" not in attributes
    assert "private claim statement" not in attributes
    assert "sensitive-path" not in attributes


class StaticSnapshotStore:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def load(self, run_id: str):
        assert run_id == self.snapshot.run_id
        return self.snapshot


def test_citation_integrity_classifies_structural_corruption_as_errors() -> None:
    memory, evidence_store, claim_store, graph = graph_services()
    ctx = context()
    ingested = _ingest(memory, ctx, "https://example.com/a", "body", "5" * 64)
    claim = graph.create_claim(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_scope_key="scope_a",
        statement="claim",
    )
    graph.relate(
        run_id=ctx.run_id,
        run_revision=ctx.run_revision,
        claim_id=claim.claim.claim_id,
        evidence_id=ingested.items[0].evidence_id,
        relation=ClaimEvidenceRelation.SUPPORTS,
    )
    evidence = evidence_store.load(ctx.run_id)
    claims = claim_store.load(ctx.run_id)
    bad_source = evidence.sources[0].model_copy(update={"source_id": "src_wrong"})
    bad_revision = evidence.revisions[0].model_copy(update={"content_hash": "0" * 64})
    corrupt_evidence = evidence.__class__.model_construct(
        schema_version=evidence.schema_version,
        run_id=evidence.run_id,
        store_revision=evidence.store_revision,
        sources=(bad_source,),
        evidence=evidence.evidence,
        revisions=(bad_revision,),
        receipts=evidence.receipts,
    )
    bad_edge = claims.edges[0].model_copy(update={"evidence_id": "ev_missing"})
    corrupt_claims = claims.__class__.model_construct(
        schema_version=claims.schema_version,
        run_id=claims.run_id,
        store_revision=claims.store_revision,
        claims=(),
        claim_revisions=(),
        edges=(bad_edge,),
        edge_revisions=claims.edge_revisions,
        receipts=claims.receipts,
    )
    result = CitationIntegrityValidator(
        claim_store=StaticSnapshotStore(corrupt_claims),
        evidence_store=StaticSnapshotStore(corrupt_evidence),
    ).validate(ctx.run_id)
    codes = {item.code for item in result.issues}
    assert result.valid is False
    assert CitationIssueCode.IDENTITY_MISMATCH in codes
    assert CitationIssueCode.CONTENT_HASH_MISMATCH in codes
    assert CitationIssueCode.DANGLING_SOURCE in codes
    assert CitationIssueCode.DANGLING_CLAIM in codes
    assert CitationIssueCode.DANGLING_EVIDENCE in codes
