# Architecture Decision Records

This file is the Phase 0 ADR log. New ADRs are appended; accepted decisions are
not silently rewritten. If a decision changes, add a superseding ADR.

## ADR-0001: Use a typed Python source-layout project

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** The system needs validated cross-component contracts, reliable
  tests, and clear separation between package code and repository utilities.
- **Decision:** Use Python 3.11+, `src/researchos`, Pydantic v2, `uv`, `pytest`,
  and `ruff`.
- **Consequences:** Packaging/import behavior is testable and schemas can evolve
  explicitly. Python 3.10 and older are unsupported.

## ADR-0002: Keep the domain independent of external providers

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** LLMs, search, browsers, retrieval stores, and observability
  services will change and must not prevent offline testing.
- **Decision:** Domain/application code depends on narrow interfaces. Concrete
  provider adapters depend inward and have contract-compatible offline mocks.
- **Consequences:** Integration mapping is explicit and replaceable, at the cost
  of maintaining interface contract tests.

## ADR-0003: Make mock and real operating modes explicit

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Silent fallback can make an apparently successful research run
  contain fixture data.
- **Decision:** Select mode explicitly. Real mode validates required provider
  configuration and fails visibly if unavailable. It never falls back to mock.
- **Consequences:** Local tests remain deterministic and production failures are
  honest. Operators must configure integrations deliberately.

## ADR-0004: Treat local structured trace as a durable run artifact

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Recovery, evaluation, and debugging require more than ephemeral
  logs or a vendor-specific observability service.
- **Decision:** Important state changes emit versioned structured events to an
  append-only local trace. Optional exporters mirror these events. Redaction
  occurs before persistence.
- **Consequences:** Runs remain auditable offline. Event schema compatibility,
  storage growth, and redaction require ongoing tests.

## ADR-0005: Persist claim-level provenance rather than transcript-only context

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Report verification requires knowing exactly which evidence
  supports or contradicts each claim.
- **Decision:** Normalize evidence and maintain atomic claims connected by typed,
  assessed, versioned edges. Raw transcripts may be artifacts but are not the
  verification model.
- **Consequences:** Citation and conflict checks become possible. Extraction and
  claim granularity introduce uncertainty that must remain visible.

## ADR-0006: Bound all autonomous correction and replanning

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Verification and replanning loops can otherwise consume unlimited
  time, tokens, cost, and tool calls.
- **Decision:** Every loop is governed by explicit iteration and resource
  budgets. Budget exhaustion produces a typed partial/unresolved outcome.
- **Consequences:** The system terminates predictably and exposes incompleteness
  instead of hiding it.

## ADR-0007: Version run artifacts and checkpoint compatibility

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Durable resume and longitudinal evaluation span evolving schemas.
- **Decision:** Persist schema versions and sanitized configuration/input hashes
  in run artifacts. Resume validates compatibility; future migrations are
  explicit.
- **Consequences:** Old runs cannot be loaded optimistically. Migration tooling
  becomes a production-readiness requirement.

## ADR-0008: Defer concrete runtime package decomposition

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Phase 0 has no proven implementation pressure for a large module
  tree, and empty shells would falsely imply completed capabilities.
- **Decision:** Create only an importable root package now. Introduce domain,
  application, interface, and adapter modules incrementally with tested Phase 1+
  behavior.
- **Consequences:** The initial tree is intentionally small. Architecture
  remains documented without speculative placeholder classes.
