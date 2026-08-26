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

## Phase 2 — Planner and validated task DAG

- TODO — Define perspective, task, dependency, policy, and DAG contracts.
- TODO — Implement DAG structural and policy validation.
- TODO — Define planner/model interfaces plus deterministic mock fixtures.
- TODO — Trace planning and bounded replan decisions.

## Phase 3 — Durable asynchronous DAG runtime

- TODO — Implement ready-queue scheduling and concurrency limits.
- TODO — Implement attempts, retry/backoff, timeout, and cancellation.
- TODO — Implement failure isolation and dependency blocking policies.
- TODO — Implement checkpoint/resume and idempotency enforcement.
- TODO — Implement budget reservation and bounded dynamic replanning.

## Phase 4 — Agent and tool adapters

- TODO — Define typed Agent and Tool protocols and capability registry.
- TODO — Add deterministic mock adapters with controllable failures.
- TODO — Add local retrieval and sandboxed Python adapters.
- TODO — Add browser/search adapter contracts; real integration remains gated.
- TODO — Test permissions, timeouts, artifacts, and no-fallback behavior.

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
