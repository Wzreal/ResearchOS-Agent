# ResearchOS Agent Architecture

**Status:** Phase 0 logical architecture. Components described below are design
targets unless explicitly marked as implemented.

## System context

ResearchOS Agent separates durable orchestration, research capabilities,
evidence/claim state, verification, and measurement. Domain code depends on
interfaces; integrations depend inward on those interfaces.

```mermaid
flowchart TD
    U[User Query] --> RM[RunManager]
    RM --> PP[Perspective Planner]
    PP --> DG[Validated Task DAG]
    DG --> EX[Async DAG Executor]

    EX --> RA[Research Agents]
    RA --> BR[Browser Adapter]
    RA --> LR[Local Retrieval Adapter]
    RA --> PY[Python Tool Adapter]
    BR --> EM[Evidence Memory]
    LR --> EM
    PY --> EM
    RA --> EM

    EM --> CG[Claim-Evidence Graph]
    CG --> SY[Synthesizer]
    SY --> RBJ[Red / Blue / Judge]
    RBJ -->|bounded corrections| CG
    RBJ --> EV[Evaluation Harness]
    EV --> FR[Final Report + Run Artifacts]

    RM <--> CP[(Checkpoint Store)]
    EX <--> CP
    RM --> TR[(Structured Trace)]
    PP --> TR
    EX --> TR
    RA --> TR
    EM --> TR
    RBJ --> TR
    EV --> TR
```

The browser, external search, vector store, LLM provider, and trace exporter are
replaceable adapters. They are not part of the domain kernel.

## Component responsibilities

### 1. RunManager

`RunManager` owns the lifecycle boundary for a research run:

- create a run ID and immutable input/configuration snapshot;
- validate operating mode and enabled adapter configuration;
- enforce legal run-status transitions;
- coordinate checkpoint loading, atomic persistence, resume, and finalization;
- expose cancellation and terminal failure semantics;
- maintain aggregate budget state;
- emit a trace event for every lifecycle change.

It does not plan research, execute tools, or judge evidence. Storage is accessed
through a `RunStore` interface. A future filesystem adapter supplies local
durability; tests use an in-memory adapter with equivalent semantics.

Conceptual run states are `CREATED`, `PLANNING`, `READY`, `RUNNING`,
`VERIFYING`, `EVALUATING`, and terminal `COMPLETED`, `PARTIAL`, `FAILED`, or
`CANCELLED`. Phase 1 will formalize the transition table in code and ADR.

### 2. Planner

`PerspectivePlanner` converts a normalized research request into perspectives
and task specifications. A separate DAG builder/validator turns these into a
graph. Validation includes unique task IDs, known dependencies, acyclicity,
capability policy, explicit outputs, and estimated budget constraints.

The planner consumes a model-provider interface rather than a vendor SDK. Its
raw response, parsed result, validation errors, and replan reason are traceable.
Replanning is a bounded runtime command, not an unrestricted recursive call.

### 3. DAG Runtime

`AsyncDAGExecutor` owns the implemented Phase 3 scheduling mechanics:

- calculate ready nodes from committed dependency outcomes;
- run tasks within concurrency and resource limits;
- apply per-task retry, backoff, timeout, and idempotency policies;
- isolate independent branch failures and propagate blocked dependencies;
- checkpoint only validated transitions and committed outcomes;
- resume from persisted state without repeating completed side effects;
- request a bounded replan when policy permits;
- reserve and consume budgets atomically;
- emit scheduling, attempt, checkpoint, budget, and failure events.

The runtime does not know provider details. It dispatches a typed
`TaskExecutionRequest` through a narrow `TaskExecutionBackend`; Phase 3 provides
only an exact-fixture offline mock, not an Agent, Tool, capability registry, or
real provider.

Ready work is ordered by descending task priority, validated topological index,
then task ID. One async coordinator is the only checkpoint writer. It reserves
budget and commits a `RUNNING` attempt before dispatch, and commits an outcome
before unlocking dependents. `ALL_SUCCESS_REQUIRED` is the sole dependency
policy: failed branches propagate stable root blockers while independent
branches continue. `ExecutorStatus.COMPLETED` means every task is terminal and
there is no runnable work; it does not mean every task succeeded. Mapping this
summary to run `COMPLETED`, `PARTIAL`, or `FAILED` remains an external lifecycle
integration responsibility.

The runtime checkpoint at `outputs/<run_id>/checkpoint.json` contains the full
validated DAG and hash, execution policy and hash, run revision, task/attempt
states, committed outcomes, budget ledger, durable replan accounting, and the
last mutation's trace outbox. Filesystem snapshots use same-directory temp
write, file fsync, atomic replace, and parent-directory fsync where supported.
Checkpoint persistence defensively rejects unsafe data; it never redacts or
changes the DAG/checkpoint because doing so would invalidate task semantics and
hashes.

For checkpoint revision `N`, the minimal protocol is trace intent fsync,
atomic snapshot, exact semantic event replay from the embedded outbox, then
trace committed fsync. The outbox stores each complete immutable `TraceEvent`
descriptor, including its original timestamp, identity, sanitized attributes,
and canonical hash. If a crash leaves the snapshot without settlement, resume
replays the stored descriptors exactly and emits a separately typed reconciled
event. It never fabricates a new original event. Only one unsettled mutation is
supported; this is not a WAL or event-sourcing system.

Execution duration is an additive task-attempt quota, not wall-clock critical
path time. A reservation is committed as measured usage when certainty is
`EXACT`, conservatively at its reported amount for `UPPER_BOUND`, and becomes
uncertain consumption for `UNKNOWN`. Timeout, cancellation, and process
interruption therefore never release unknown usage. Full release is limited to
work proven not to have been dispatched. Honest backend overrun is recorded as
a breach and pauses scheduling.

Run cancellation enters through a run-level controller. Each attempt receives
only a read-only cancellation signal. New tasks stop, queued work is cancelled,
and an already dispatched backend is given until its original timeout to
cooperate. A stable operation key includes `operation_version` and survives
retry/resume; each consumed attempt has a separate attempt key.

### 4. Agent and Tool

An `Agent` interprets a typed research task and chooses permitted tools. A
`Tool` performs one bounded capability with validated input/output. Planned
cross-cutting requirements are:

- stable capability name and schema version;
- explicit mode and adapter identity;
- deadlines, cancellation, and resource policy;
- structured results, artifacts, and errors;
- no direct writes to run state outside runtime-owned interfaces;
- redacted trace events for invocation and completion.

Mocks reproduce contracts, latency/failure controls, and fixtures. They do not
silently replace real adapters.

Phase 4 implements this boundary through an asynchronous `Agent` decision
port, a one-invocation `Tool` port, a default-deny capability registry, and an
`AgentRunner` that bridges the Phase 3 `TaskExecutionBackend`. Each Agent or
Tool await races the attempt deadline and cancellation signal. The runner
maintains one local usage accumulator and enforces task hard limits; Phase 3
remains the only durable budget owner.

Tool logical identity is independent of model-generated call identity. It is a
canonical hash of the task operation key, Agent step, capability, Tool and
adapter IDs, Tool operation version, and safe canonical input hash. The model's
`tool_call_id` is correlation metadata only. Agent/Tool semantic events append
directly to the trace sink and fail closed; Phase 4 adds neither a second
checkpoint outbox nor a durable Tool journal.

The local retrieval adapter uses deterministic stdlib BM25 over a validated
JSONL corpus. The Python adapter runs trusted supported code in a bounded
subprocess and publishes only declared artifacts through an atomic store. Its
import allowlist is a compatibility policy, not a hostile-code security
boundary. Browser and search are provider-independent typed contracts with
exact offline fixtures; real adapters remain gated.

### 5. Retriever

Retrieval is a domain capability with adapters for local corpora and later
external systems. Requests express query, filters, limit, and provenance needs;
responses contain scored candidates and retrieval metadata. Indexing and
retrieval are separate responsibilities. The architecture does not assume
Milvus or any specific embedding provider.

### 6. Evidence Memory

`EvidenceMemory` stores normalized evidence and exposes deterministic lookup and
deduplication. An evidence record will include source identity, locator,
retrieved time, content/content hash, extraction context, adapter, and schema
version. It must preserve original provenance even when normalized.

The Claim-Evidence Graph stores atomic claims and typed edges such as
`SUPPORTS`, `CONTRADICTS`, and `CONTEXTUALIZES`, including assessment metadata
and revision history. Persistence is behind interfaces so local Phase 5 storage
does not commit the project to a future database.

### 7. Verification

Verification operates on claims and evidence rather than free-form transcripts:

- conflict detection nominates claim/evidence groups for review;
- Red challenges coverage, source quality, and logical support;
- Blue answers challenges using existing or newly authorized evidence;
- Judge records a structured disposition and allowed correction;
- a verification policy caps rounds, time, tokens, cost, and new tool calls.

Unresolved conflict is an output state, not an exception to hide. These roles
are not implemented in Phase 0.

### 8. Evaluation

The evaluation harness consumes immutable run artifacts and versioned fixtures.
It has independent evaluator interfaces for retrieval, trajectory, and report
quality. It records evaluator/configuration versions, inputs, outputs, skipped
reasons, and uncertainty. Ablations must hold datasets and evaluation policy
constant while varying declared system components.

Evaluation must not mutate the run being evaluated. Candidate metrics are
defined in `EVALUATION.md`.

### 9. Observability

The durable source of truth is a local, append-only structured trace. Optional
exporters may mirror events but cannot be the only record. Every event will
carry at least:

- schema version, event ID, event type, timestamp;
- run ID and, when applicable, task/attempt/agent/tool IDs;
- previous and next state for state transitions;
- correlation/causation IDs;
- sanitized attributes, duration, budget delta, and error classification.

Important events include run and task transitions, scheduling decisions,
planner/replan decisions, tool calls, evidence/claim mutations, checkpoint
commits, verification outcomes, budget decisions, evaluation results, and
terminal publication. Secret redaction happens before persistence.

## Primary data flow

1. The request enters `RunManager`; validated inputs and mode are persisted.
2. The planner produces perspectives and a candidate DAG; the validator either
   commits it or returns explicit errors.
3. The executor selects ready nodes, reserves budget, and dispatches typed work.
4. Tools return structured observations and artifacts; evidence ingestion
   normalizes provenance and updates claim links.
5. Checkpoints commit task outcomes and budget state before dependents advance.
6. Synthesis reads the graph and produces a claim-addressable draft.
7. Bounded verification may add tasks/evidence or revise claim dispositions.
8. Evaluation reads frozen artifacts; finalization writes the manifest and
   exposes complete, partial, or failed status.

## Failure and recovery model

- Expected operational failures are typed data, not swallowed exceptions.
- A task attempt may fail without failing independent DAG branches.
- Dependents become blocked only according to declared dependency policy.
- Checkpoints use atomic replace/commit semantics and schema versions.
- Resume validates the input/configuration hash and checkpoint compatibility.
- External side effects require an idempotency key or an explicit non-retryable
  policy.
- Trace export failure is visible but never erases the local durable event.

## Package direction

The planned dependency direction is:

```text
domain contracts <- application services <- interfaces <- adapters/entrypoints
```

The domain layer must not import vendor SDKs or filesystem/network adapters.
Implemented Phase 1-3 boundaries live under `domain`, `application`,
`interfaces`, and `adapters`. Future packages are still introduced only when a
phase has tested behavior rather than as empty capability shells.
