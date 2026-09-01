# ResearchOS Agent Task Board

Allowed states: **TODO**, **DOING**, **DONE**. A task becomes DONE only when its
acceptance criteria and required tests pass. This file tracks implementation,
not aspiration.

## Phase 0 — Architecture and minimal engineering skeleton

- DONE — Define project goals, non-goals, user flow, operating modes, output
  contract, and phase acceptance criteria.
- DONE — Define logical architecture, data flow, failure model, and component
  boundaries.
- DONE — Establish ADR baseline and evaluation framework without experimental
  claims.
- DONE — Create Python 3.11+ source-layout package and import test.
- DONE — Configure uv, Pydantic v2, pytest, and ruff.
- DONE — Verify `uv sync`, `uv run pytest`, and `uv run ruff check .`.

## Phase 1 — Domain contracts and RunManager lifecycle

- DONE — Define Pydantic contracts for configuration, modes, IDs, run state,
  budgets, errors, artifacts, and trace events.
- DONE — Decide and document the legal run-state transition table.
- DONE — Define `RunStore`, `Clock`, and trace sink interfaces.
- DONE — Implement and test an in-memory store and atomic filesystem store.
- DONE — Implement run create/load/transition/finalize/resume orchestration.
- DONE — Add offline tests for invalid transitions, corruption, redaction, and
  real-mode configuration failure.
- DONE — Harden create crash outcome reporting and persisted trace identity and
  redaction integrity checks.

## Phase 2 — Planner and validated task DAG

- DONE — Define perspective, task, dependency, policy, and DAG contracts.
- DONE — Implement DAG structural and policy validation.
- DONE — Define planner/model interfaces plus deterministic mock fixtures.
- DONE — Trace planning and bounded replan decisions.
- DONE — Harden graph-metric safety, request provenance, malformed-error
  sanitization, and explicit expected-output identity after PR review.

## Phase 3 — Durable asynchronous DAG runtime

- DONE — Implement deterministic ready-queue scheduling and concurrency limits.
- DONE — Implement attempts, retry/backoff, timeout, and explicit run
  cancellation.
- DONE — Implement `ALL_SUCCESS_REQUIRED` failure isolation and root-cause
  dependency blocking.
- DONE — Implement atomic checkpoint/resume, immutable trace outbox recovery,
  and two-level idempotency keys.
- DONE — Implement additive budget reservation, usage certainty, overrun
  handling, durable bounded replan requests, and trusted planner-lineage
  restoration.
- DONE — Add deterministic backend fixtures, fault-injection coverage, and a
  minimal Python 3.11 CI workflow.
- DONE — Harden runtime replan consumption after restart, completion-driven
  settlement, durable retry timing, full ready-budget scanning, strict runtime
  transitions, and timeout/cancellation terminal-cause consistency after PR
  audit.

## Phase 4 — Agent and tool adapters

- DONE — Define typed asynchronous Agent and Tool protocols, bounded
  `AgentRunner`, Phase 3 backend bridge, and default-deny capability registry.
- DONE — Add deterministic exact-fixture Agent and Tool adapters with
  controllable failure, cancellation, and non-return behavior.
- DONE — Add deterministic local BM25 retrieval and trusted-code bounded Python
  subprocess adapters with explicit artifact publication.
- DONE — Add browser/search typed contracts and generic offline Tool fixtures;
  real provider integration remains gated to Phase 9.
- DONE — Test authorization, independent Agent/Tool deadlines and cancellation,
  hard limits, stable operation identity, trace failures, artifacts, adapter
  mode validation, and no-fallback behavior.
- DONE — Complete the Phase 4 GitHub reference and independent test-coverage
  audits without expanding into Phase 5 or real providers.
- DONE — Harden in-flight Agent usage certainty, Tool result/provenance and
  observation bounds, streaming Python output limits, non-blocking bounded
  local retrieval, and final output media-type validation after PR audit.

## Phase 5 — Evidence Memory and Claim-Evidence Graph

- DONE — Define evidence, source, claim, edge, receipt, and immutable revision
  schemas with stable entity identity.
- DONE — Implement defensive in-memory and atomic deterministic JSONL snapshot
  stores, canonical hashing, deduplication, and revision semantics.
- DONE — Implement trusted Browser/Search/Local Retrieval extraction and ingest
  eligible successful observations before final Agent-result mapping.
- DONE — Implement typed claim-evidence relationships, bounded graph queries,
  tombstones, conflict-candidate detection, and structural citation integrity.
- DONE — Add Phase 5 persistence, idempotency, provenance, trace-failure,
  integration, staleness, and corruption tests plus GitHub reference review.
- DONE — Complete the Phase 5 semantic/test-coverage audit, current-valid
  conflict hardening, defensive snapshot validation, and failed-Agent
  observation retention checks.
- DONE — Harden PR #6 portability, conservative URL/text identity, citation
  source/content pinning, snapshot/receipt invariants, claim-create revision
  semantics, and deterministic backend ingestion ordering.

## Phase 6 — Synthesis and bounded verification

- DONE — Implement frozen, whole-item evidence-grounded synthesis contracts
  and deterministic mock adapter.
- DONE — Implement bounded Red, Blue, and Judge roles with stable structured
  identities, action validation, and derived structural support.
- DONE — Enforce rounds, claims, findings, evidence, bytes, hard limits,
  deadline, and cancellation without adding a second runtime or budget ledger.
- DONE — Publish authoritative verification JSON before deterministic Markdown,
  support zero-model-call replay, and preserve removed/unresolved claim state.
- DONE — Harden PR #7 current-valid edge semantics, Judge-owned finding
  dispositions, finding-bound Blue actions, citation completeness, reentrant
  coordination, authoritative round audit data, Markdown ownership, and
  committed-state error reporting.

## Phase 7 — Evaluation harness

- DONE — Add bounded, versioned evaluation datasets with canonical filesystem
  and in-memory loaders plus explicit reference levels and execution conditions.
- DONE — Implement read-only artifact freezing, fail-closed case/run binding,
  homogeneous SUT pins, and independent planning/execution/evidence/citation/
  verification recomputation.
- DONE — Add exact reference evaluators and the optional provider-independent,
  MOCK-only exact-fixture judge with a global invocation call bound.
- DONE — Persist typed metric-definition snapshots, case artifacts, and
  authority-last EvaluationRuns with separate semantic and physical hashes.
- DONE — Implement deterministic aggregation, definition-safe regression
  comparison, and provenance-preserving ablation validation.
- DONE — Cover replay, tamper detection, unavailable semantics, compatibility,
  model bounds, comparison thresholds, and Phase 2-6 regression behavior.
- DONE — Harden one-read corrupt artifact isolation, strict SUT provenance,
  Decimal metric semantics, bounded dispatch, independent current-edge/citation
  recomputation, canonical ablation identity, and immutable concurrent
  first-writer authorities.

## Phase 8 — Observability and resilience hardening

- DONE — Add a bounded durable verification-operation checkpoint with stable
  request proofs, validated response replay, UNKNOWN interruption semantics,
  and authority-first lifecycle reconciliation.
- DONE — Implement in-memory and filesystem operation stores with shared-lock
  in-process CAS, defensive validation, atomic replace, and typed corruption.
- DONE — Add descriptor-level local trace append-once, correlated observation
  envelopes, deterministic critical-outbox capacity, and independently rebuilt
  verification lineage.
- DONE — Add a nonblocking bounded optional exporter dispatcher with timeout,
  failure/backpressure isolation, local-only diagnostics, and no fallback.
- DONE — Cover model-call crash windows, response/authority replay, concurrent
  CAS and recovery attempts, legacy authority bootstrap, redaction, exporter
  isolation, cancellation/deadline, and complete Phase 1-7 regression.
- DONE — Harden immutable READY publication replay, hard-limit compatibility,
  verification generations/supersession, best-effort trace settlement, terminal
  outcomes, checkpoint byte ceilings, diagnostic thread isolation, SUPPORTS
  lineage, terminal Run replay, and recovery-event ordering.
- DONE — Enforce the Phase 3 terminal checkpoint gate, unknown provider
  transport outcomes, off-loop delivery-drop diagnostics, unresolved-generation
  exclusion, one-time predispatch terminals, and terminal trace taxonomy.
- DONE — Publish ADR-0024, Phase 8 design documentation, and the recovery
  runbook.

## Phase 9 — Real integrations and production readiness

- DONE — Implement Phase 9A immutable REAL composition, central Run mutation
  guard, process-local binding, bounded OpenAI-compatible transport, and
  DeepSeek Planning/Agent/Verification adapters with deterministic offline
  tests.
- DONE — Add Phase 9A optional dependency isolation, core/all-extras CI split,
  non-persistent environment/secret settings, and zero-cost/read-only doctor
  basics.
- DONE — Harden the DeepSeek V4 request profile, per-model pricing upper
  bounds and usage certainty, dispatch-time composition revalidation,
  retryability preservation, generic secret validation, and client close.
- DONE — Fail closed on non-stop provider completions, freeze adapter-owned
  pricing safety profiles, and enforce provider-call reservation compatibility
  at existing Run and Agent budget boundaries.
- DONE — Correct DeepSeek V4 reservation safety data to the reviewed 1M context,
  pin the 384K output maximum and USD billing currency, and reject currency or
  model-limit drift before dispatch.
- TODO — Integrate selected browser/search and retrieval providers.
- TODO — Integrate optional observability backend.
- TODO — Complete security, migration, end-to-end, and release gates.
- TODO — Publish measured results, limitations, costs, and operator runbook.
