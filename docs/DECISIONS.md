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

## ADR-0009: Version Phase 1 contracts and enforce a strict run lifecycle

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Run persistence and future runtime components need stable data
  contracts, an unambiguous lifecycle, and deterministic budget values without
  prematurely defining a Phase 2 DAG or Phase 3 task runtime.
- **Decision:** Version the Phase 1 Pydantic contracts at schema version 1 and
  reject unknown fields. Use the strict lifecycle `CREATED`, `PLANNING`,
  `READY`, `RUNNING`, `VERIFYING`, `EVALUATING`, followed by terminal
  `COMPLETED`, `PARTIAL`, `FAILED`, or `CANCELLED` according to the transition
  table enforced by `RunManager`. A Phase 1 `TaskRecord` contains identity and
  timestamps only. Omitted budget constraints resolve at run creation to
  effective limits of 3,600 seconds, 100,000 tokens, 10,000,000 USD
  microunits, and 100 tool calls; `RunState` never stores unset limits.
  Pydantic validates that deadlines are timezone-aware, while `RunManager`
  compares deadlines with its injected `Clock`. `load()` is strictly read-only;
  `resume()` validates input/configuration hashes and records `run.resumed`.
- **Consequences:** Future runtime code receives deterministic configuration
  and cannot bypass lifecycle stages. Optional stage skipping, task execution
  fields, budget accounting, and schema migrations require later decisions.

## ADR-0010: Use a minimal intent-state-committed crash protocol

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** `run_state.json` and `trace.jsonl` cannot be updated by one
  filesystem transaction, but lifecycle state must remain recoverable and
  auditable after a crash.
- **Decision:** For target revision `N`, append and fsync
  `transition_intent(N, transition_id, from, to)`, atomically write the new
  state through a same-directory temp file, file fsync, `os.replace`, and
  parent-directory fsync where supported, then append and fsync
  `transition_committed(N, transition_id)`. Revisions increase by exactly one;
  creation is revision 0 from no prior state to `CREATED`. Recovery applies
  only these tail rules:
  1. matching state and committed event are complete;
  2. intent newer than state is uncommitted and ignored;
  3. state with matching intent but no committed event remains committed and
     receives one idempotent, separately typed `transition_reconciled` event;
  4. committed/reconciled trace newer than state is corruption and fails.
  A matching transition ID is mandatory. Reconciliation never writes an
  original committed event and never re-executes the transition. A single torn
  final JSONL event is truncated to the last complete newline and fsynced
  before another append; damage to any complete middle line is corruption.
  Phase 1 assumes one process writer per run.
- **Consequences:** Known tail crash windows recover deterministically without
  rolling state backward. The trace is inspected for consistency but is not a
  WAL, transaction log, event-sourcing model, or source for rebuilding state.
  Windows parent-directory fsync remains best effort; multi-process locking and
  general log repair are deferred.

## ADR-0011: Reject real mode and redact before Phase 1 persistence

- **Status:** Accepted
- **Date:** 2026-08-26
- **Context:** Phase 1 has no real provider adapter, and secrets must not enter
  durable state, hashes, traces, or error records.
- **Decision:** Accept `real` as a schema value but reject real-mode create and
  resume before persistence until a real adapter exists. Never fall back to
  mock. Prepare persistent input in the fixed order normalize, redact,
  canonical serialize, then SHA-256; persistent hashes never derive from text
  already identified as secret. `RunManager` performs redaction at the
  persistence boundary. Stores and trace sinks do not modify domain objects;
  they defensively reject objects that still require redaction. Credentials and
  provider settings are not fields in `RunConfig`.
- **Consequences:** Mock lifecycle tests remain offline and honest, persisted
  input hashes are safe and reproducible, and adapter bugs cannot silently
  transform domain data. Phase 4 must add adapter-specific real-mode validation
  through a superseding or additional ADR.

## ADR-0012: Expose uncertain create outcomes and validate trace ownership

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** A create operation can fail after `run_state.json` becomes
  visible, leaving a durable run whose identifier must not be lost. A valid
  trace schema alone also does not prove that an event belongs to the run
  directory from which it was read or that the event was safely redacted.
- **Decision:** Every `StatePersistenceError` carries the affected `run_id`.
  Its `state_replaced` field means `os.replace` returned successfully and the
  new snapshot was observable before a later write or durability step failed;
  callers must therefore treat the state as possibly durable and inspect it by
  that run ID. `state_replaced=false` means replacement did not occur.
  Every `TraceCommitError` carries `run_id`, transition ID, and revision, while
  `state_committed=true` means RunStore returned successfully before the
  committed trace append failed or could not be confirmed. It does not assert
  that the committed event is absent, because a failure during append may have
  occurred after bytes reached the file. Filesystem trace reads validate every
  event's run ID against the requested run and reject persisted events that
  still require redaction. The unused `run.resume_rejected` event contract is
  removed rather than implying unimplemented emission behavior.
- **Consequences:** Callers can always locate and reconcile a possibly created
  run without changing the intent-state-committed protocol. Cross-run trace
  contamination and unsafe persisted trace data fail as corruption rather than
  being accepted or silently rewritten.
