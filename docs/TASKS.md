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

- TODO — Define evidence, source, claim, edge, and revision schemas.
- TODO — Implement local persistence, hashing, and deduplication.
- TODO — Implement typed claim-evidence relationships and graph queries.
- TODO — Implement conflict-candidate detection and citation integrity checks.

## Phase 6 — Synthesis and bounded verification

- TODO — Implement evidence-grounded synthesis contract and adapter.
- TODO — Implement Red, Blue, and Judge roles with structured outcomes.
- TODO — Enforce correction-round and resource budgets.
- TODO — Surface unsupported and unresolved claims in report output.

## Phase 7 — Evaluation harness

- TODO — Add versioned datasets and deterministic fixture loader.
- TODO — Implement retrieval, trajectory, and report evaluators.
- TODO — Implement controlled ablation configuration and comparison.
- TODO — Record metric provenance, skipped reasons, and uncertainty.

## Phase 8 — Observability and resilience hardening

- TODO — Complete versioned trace schemas and correlation coverage.
- TODO — Add redaction, fault-injection, crash-recovery, and load tests.
- TODO — Add optional exporter interface and explicit exporter failures.
- TODO — Publish operator diagnostics and recovery runbook.

## Phase 9 — Real integrations and production readiness

- TODO — Integrate selected real LLM provider through its adapter.
- TODO — Integrate selected browser/search and retrieval providers.
- TODO — Integrate optional observability backend.
- TODO — Complete security, migration, end-to-end, and release gates.
- TODO — Publish measured results, limitations, costs, and operator runbook.
