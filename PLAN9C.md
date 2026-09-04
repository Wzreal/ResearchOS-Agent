# Phase 9C — Zilliz Cloud Free BM25 Retrieval

## Scope

Phase 9C adds exactly one read-only REAL capability, `managed_retrieval`, backed
by the Zilliz Cloud **Free cluster** serving-endpoint profile and Milvus built-in
BM25. It queries an operator-provisioned collection and returns existing
`LocalRetrievalResult` records for Phase 5 ingestion. It adds no ingestion,
embedding, dense or hybrid search, reranker, second provider, observability,
REAL E2E, or Phase 9D/9E work.

## Existing boundaries

- Local retrieval remains the deterministic root-confined JSONL BM25 adapter.
- Tavily discovers Web candidates and its summaries are never Evidence; Browser
  fetches validated source bodies.
- `AgentTaskExecutionBackend` sends successful observations to the existing
  extractor and Evidence Memory. `LocalRetrievalResult` is already an eligible
  local-document evidence input.
- REAL capability construction, authorization, admission, trace, retry and
  lifecycle authority remain respectively in `RealIntegrationFactory`,
  `RealToolDispatchAuthorizer`, `AgentRunner`, Phase 3 and Phase 5.

## Frozen provider profile

Only `zilliz_cloud_free_serverless_bm25_v1` is supported:

- Endpoint is exactly the canonical no-port HTTPS serving form
  `https://<cluster-id>.serverless.<region>.vectordb.zillizcloud.com/`.
  Userinfo, query, fragment, explicit port, private endpoints, global
  endpoints, on-demand endpoints, Dedicated endpoints and arbitrary Milvus URIs
  are rejected before credential lookup.
- It requires a pinned Zilliz token credential slot. The adapter sequence is
  authorization/composition validation, endpoint validation, credential lookup,
  then client creation and one search RPC.
- Free-cluster query cost has the proven fixed monetary upper bound `0 USD`.
  The reservation therefore uses `cost_microunits=0`, `USD`, one Tool call and
  the frozen total timeout. Quota exhaustion is a provider failure, not a cost
  estimate. Serverless paid, on-demand and Dedicated profiles have variable
  usage/resource charges without a per-call immutable upper bound and are
  unsupported/fail closed. No second budget ledger is introduced.
- The collection is operator-provisioned and read-only to ResearchOS. One
  logical invocation issues exactly one BM25 search RPC; no describe/list/schema
  inspection RPC is permitted.

## Contracts

Reuse `LocalRetrievalRequest`, `LocalRetrievalHit`, `LocalRetrievalResult`,
`ToolInvocationRequest` v1 and all Phase 3/5 contracts unchanged. Add a frozen
Zilliz policy containing endpoint identity, collection ID, fixed `anns_field`,
fixed output-field list, limits/timeouts, token slot, free-profile reservation,
and a collection schema contract/hash.

The fixed collection output schema contains `document_id`, `chunk_id`,
`locator`, `content`, `content_hash`, and bounded safe metadata. The adapter
recomputes SHA-256 from the original returned UTF-8 content bytes and compares
it with the distinct stored `content_hash` field. Any mismatch, duplicate chunk,
unsafe locator, malformed metadata or bound violation is
`retrieval_response_invalid`; only validated raw chunks enter the existing
result/Evidence path.

Add `agent-tool-decision-v3` only for compositions containing
`managed_retrieval`; it supports Search, Browser and Local Retrieval calls.
Phase 9A direct v1 and Phase 9B web-only v2 prompt/schema/model identities are
unchanged. The v3 profile uses the existing accumulated provider admission.

## Implementation and tests

Add a lazy `pymilvus` adapter behind a narrow injected transport, the frozen
policy/settings/factory wiring, v3 Agent schema selection, doctor dependency and
credential validation, ADR/design/task updates, and deterministic tests.

Tests cover endpoint rejection before secret access, authorization, reservation
admission, exact one-RPC behavior, Free reservation semantics, unavailable and
malformed providers, stored-hash mismatch, timeout/cancellation unknown usage,
Phase 3 retry delegation, Evidence ingestion, optional-extra isolation, and
Phase 1–9B regression. Tests make no remote or paid calls.

## Acceptance

Run core pytest after `uv sync`, all pytest after `uv sync --all-extras`, Ruff,
and `git diff --check`. The adapter must have no fallback and must not mutate the
remote collection.
