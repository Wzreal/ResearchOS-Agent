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

## ADR-0013: Separate candidate plans from canonical validated task DAGs

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** A planning model must be allowed to return structurally parseable
  but semantically invalid candidates so that dependency, policy, capability,
  and budget failures remain typed and testable. Treating model output as an
  executable graph would bypass that trust boundary.
- **Decision:** Phase 2 uses versioned provider-independent contracts for
  perspectives, research tasks, typed dependencies, resource estimates,
  planner metadata, candidates, validation results, and `TaskDAG`. Candidate
  contracts admit semantic errors; the deterministic validator emits stable,
  sorted issue codes and computes a lexicographically stable topological order,
  graph depth, aggregate token/cost/tool estimates, and dependency-critical-path
  duration. It validates candidate run and plan identity and requires
  `PlanningModelResponse.planning_model_id` to equal the candidate planner
  metadata model ID. Only a candidate with zero validation errors can be
  normalized into a strict `TaskDAG`. Duration feasibility uses the longest
  dependency path; other resource feasibility uses the sum across all tasks.
  Capability authorization is a subset check against the run configuration and
  does not introduce a capability registry. Phase 2 returns the DAG and records
  sanitized planning trace summaries but does not persist it or alter
  `RunState`.
- **Consequences:** Invalid model output has deterministic diagnostics and a
  validated DAG has canonical hashing behavior without `networkx` or provider
  SDKs. Model quality, estimate accuracy, DAG persistence, checkpointing,
  scheduling, and execution remain outside Phase 2.

## ADR-0014: Use one-shot planning with in-flow bounded replan lineage

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** Replanning must be bounded without accepting a caller-supplied
  integer that can be reset, while Phase 2 explicitly excludes databases,
  checkpoint resume, and durable runtime accounting. Model transport failures
  must also remain distinct from candidate validation failures.
- **Decision:** `PerspectivePlanner` requires a run already in `PLANNING`, calls
  the provider-independent `PlanningModel` at most once per `plan()` or approved
  `replan()` invocation, and never performs a recursive repair loop. Every
  result produces a `ReplanContext` containing its plan ID, cumulative replan
  count, and prior decision ID. The service keeps these cursors in memory,
  validates the exact prior result/context/decision lineage, and consumes an
  approved prior cursor before making the next model call, preventing caller
  resets and repeated forks within one application flow. The deterministic mock
  selects fixtures by normalized query, replan count, and reason code, with no
  default success or real-to-mock fallback.

  `PlanningResult.validation` is optional with status-specific invariants:
  `VALIDATED` requires a valid result and DAG; `INVALID` requires an invalid
  result and no DAG; `MALFORMED` requires an invalid result containing
  `candidate_malformed`; and `MODEL_ERROR` requires `validation=None` plus a
  planning error. Model failures never receive a fabricated validation result.
  Planning and replan decisions emit sanitized structured trace events without
  raw provider payloads or queries.
- **Consequences:** Replan bounds and provenance are reliable for one live
  service flow and deterministic offline tests can model invalid-to-valid
  repair. Process restart loses the lineage registry by design. Durable replan
  accounting, DAG storage, runtime checkpoint/resume, and budget reservation
  are Phase 3 responsibilities.

## ADR-0015: Make Phase 2 validation provenance and partial metrics explicit

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** Review of the Phase 2 contracts found that partial graph metrics
  could appear authoritative after unsafe structural failures, planning
  requests omitted important run context, malformed Pydantic diagnostics could
  retain candidate input, and expected outputs lacked stable identity.
- **Decision:** `ValidationResult` exposes topological order, graph depth, and
  critical-path duration only when the candidate graph is structurally safe.
  Duplicate or invalid task IDs, unknown/self/duplicate dependencies, and
  cycles suppress all three values and suppress duration-budget feasibility;
  aggregate token, cost, and tool-call checks remain independent. Validation is
  valid exactly when it contains no `ERROR` issue, so warnings do not invalidate
  a candidate. Expected outputs use an explicit task-local unique `output_id`
  and canonical ordering by that ID, plus description and media type.

  Every `PlanningRequest` records run revision, source policy, output format,
  and an injected-clock request timestamp. Planning results and validated DAGs
  carry the same run revision, while trace events already record it in their
  revision field. Candidate parse failures use Pydantic errors with input and
  URLs excluded, then retain only allowlisted locations, stable error types,
  and a constant sanitized message. Raw candidate input and provider responses
  never enter validation issues or trace.
- **Consequences:** Consumers can distinguish unavailable graph metrics from
  zero-valued metrics and correlate a plan with its exact run snapshot without
  adding persistence. Diagnostic detail is intentionally less verbose to
  guarantee that malformed free text is not retained. Durable plan provenance,
  persistence, and recovery remain Phase 3 work.

## ADR-0016: Use a deterministic single-writer asynchronous DAG runtime

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** Phase 3 must execute a validated DAG concurrently while keeping
  scheduling decisions, dependency propagation, retries, and cancellation
  deterministic and recoverable without introducing Phase 4 agents or tools.
- **Decision:** A single asyncio coordinator is the only runtime-state writer.
  Ready tasks are ordered by descending priority, validated topological index,
  and task ID. `ALL_SUCCESS_REQUIRED` is the only dependency policy; root
  failure IDs propagate through blocked descendants and independent branches
  continue. Attempts use explicit retry schedules without jitter, fixed
  timeouts, and a run-level cancellation controller that supplies read-only
  per-attempt signals. Already dispatched non-cooperative work is allowed until
  its original timeout. `ExecutorStatus.COMPLETED` means all tasks are terminal
  and no work is runnable, not that every task succeeded. Run terminal-status
  mapping remains outside the executor.
- **Consequences:** Offline behavior and trace order are reproducible and no
  multiprocess lock or scheduler dependency is required. Phase 3 supports one
  executor writer per run; distributed leases, provider interruption, and run
  lifecycle finalization are deferred.

## ADR-0017: Use atomic checkpoints with a one-mutation immutable trace outbox

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** A checkpoint snapshot and append-only trace cannot be one
  filesystem transaction. Recovery must not regenerate timestamps or semantic
  event content after a crash, and the solution must remain smaller than a WAL.
- **Decision:** Checkpoints persist the full DAG/policy snapshots and hashes,
  run identity/revision, runtime states, outcomes, budget, replan accounting,
  and one `last_mutation` outbox. Every outbox entry is a complete immutable
  `TraceEvent` descriptor containing event ID/type, original timestamp,
  run/revision, correlation/causation, all other event fields, sanitized
  attributes, and its canonical event hash. Revision `N` commits as:
  checkpoint intent and fsync; same-directory temp write, temp fsync, atomic
  replace, and parent fsync; exact outbox event appends; checkpoint committed
  and fsync. A new mutation cannot start until the prior one is settled.

  On recovery, an intent newer than the snapshot is ignored. A snapshot with a
  matching intent but missing or partial semantic settlement is authoritative:
  missing descriptors are replayed exactly and a distinct reconciled event is
  appended. A committed/reconciled revision newer than the snapshot, missing
  matching intent, event-ID/content conflict, schema/hash mismatch, or settled
  snapshot missing an outbox event is corruption. Reconciliation is idempotent
  and never writes a fabricated original committed event. Persistence invokes
  `assert_safe_model(checkpoint)` and rejects rather than silently redacting the
  DAG or checkpoint.
- **Consequences:** Completed outcomes are never redispatched merely because a
  trace append crashed, while trace content retains its original identity and
  time. Only the last mutation can be repaired; this is explicitly not a WAL,
  transaction log, or event-sourcing design. Windows parent-directory fsync is
  still best effort.

## ADR-0018: Make idempotency, uncertain usage, and durable replan bounds explicit

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** Retry/resume can repeat externally visible operations, provider
  usage can be unknown after timeout/cancellation/crash, and Phase 2's in-memory
  replan lineage otherwise creates a restart gap.
- **Decision:** Each task execution policy declares `operation_version` and an
  idempotency mode. The stable operation key hashes run, DAG identity/hash,
  task ID, and operation version; a distinct attempt key additionally hashes
  the monotonically consumed attempt number. Automatic retry requires an
  idempotent operation. A recovered `RUNNING` attempt becomes `INTERRUPTED` and
  consumes its attempt number; non-idempotent or exhausted work fails.

  Runtime budget is an additive quota. `EXACT` usage commits the measured
  amount, `UPPER_BOUND` commits the conservative reported amount, and `UNKNOWN`
  converts the full reservation to uncertain consumption. Unknown timeout,
  cancellation, or interruption usage is never released; only work proven not
  dispatched receives full release. Reported overrun is committed honestly,
  marks the ledger breached, and pauses scheduling.

  A replan request reserves a durable slot when written to the checkpoint,
  stores the pending request, and pauses the executor; Phase 3 does not invoke
  the planner or apply a replacement DAG. `PerspectivePlanner` exposes the
  narrow `restore_trusted_lineage` boundary so an external coordinator may
  restore only a `ReplanContext` already validated against checkpoint schema,
  hashes, and run identity. The boundary is idempotent and rejects conflicting
  live lineage. This supersedes ADR-0014's Phase 2-only restart limitation
  without changing its in-flow validation rules. No database is added.
- **Consequences:** Retry safety and conservative cost accounting are explicit
  across restart, and pending replan/accounting can survive a process. Applying
  a newly validated DAG, updating run lifecycle budget snapshots, and
  provider-backed reconciliation of uncertain consumption are deferred.

## ADR-0019: Make Phase 3 scheduling progress and runtime replan consumption explicit

- **Status:** Accepted
- **Date:** 2026-08-27
- **Context:** Phase 3 audit found that a restored runtime replan cursor could
  not be consumed while the run remained `RUNNING`, batch settlement delayed a
  fast sibling behind the slowest attempt, and retry/budget edge cases could
  incorrectly report no work or budget exhaustion. Snapshot invariants alone
  also did not state the legal runtime transition graph.
- **Decision:** `PerspectivePlanner` exposes a narrow trusted-runtime replan
  operation that accepts only `RUNNING` run state and an exact lineage cursor
  previously installed by `restore_trusted_lineage`. It verifies run identity
  and count limits, consumes the cursor before exactly one model call, returns
  a genuine planning result, and neither applies a DAG nor changes run
  lifecycle state.

  The single-writer coordinator keeps an in-memory set of dispatched attempts
  and waits for `FIRST_COMPLETED`. Simultaneous completions settle in stable
  dispatch-sequence/task-ID order, but each completed attempt receives its own
  checkpoint immediately and frees its slot before the ready set is recomputed.
  Ready scanning examines the complete deterministic order until every slot is
  filled or every candidate is unreservable. Budget exhaustion is declared
  only with no running work, no retry that can become eligible, and no
  reservable ready task.

  `RETRY_WAIT` is a durable scheduler state. `next_eligible_at` is the sole
  retry deadline: due retries become ready together, future retries cause one
  wait for the earliest remaining interval, restart never reapplies the full
  backoff, and run cancellation interrupts the wait. Explicit legal task and
  attempt transition maps are checked before executor mutations. Attempt
  terminal trace events follow the attempt's actual terminal cause; therefore
  a timeout observed during run cancellation emits `ATTEMPT_TIMED_OUT` even
  though the enclosing task becomes `CANCELLED`.
- **Consequences:** Checkpoint progress is observable at attempt granularity,
  durable retries retain their original schedule, independent affordable work
  is not starved by expensive tasks, and restart can consume a pending runtime
  replan without fabricating prior planning state. The live dispatch set is
  intentionally process-local; checkpoint recovery still converts persisted
  running attempts to interruption according to ADR-0018. Replacement-DAG
  application and run lifecycle integration remain outside Phase 3.

## ADR-0020: Bound Agent decisions and use explicit capability adapters

- **Status:** Accepted
- **Date:** 2026-08-28
- **Context:** Phase 4 must connect the durable Phase 3 task backend to Agent
  decisions and local Tools without giving models authority over permissions,
  idempotency identity, timeouts, or budget settlement. Tool execution may
  finish before its terminal trace append, while a Python subprocess cannot be
  treated as a security boundary merely because its duration and imports are
  bounded.
- **Decision:** The Agent port is asynchronous and receives the same read-only
  per-attempt cancellation signal as Tool invocations. `AgentRunner` races each
  Agent decision and Tool invocation independently against cancellation and the
  attempt deadline. Non-returning, cancelled, and deadline-exceeded decisions
  have distinct stable failures. The loop has explicit Agent-step and
  Tool-call bounds and maintains one local duration/token/cost/Tool-call usage
  accumulator. The effective Tool-call limit is the minimum of runner policy
  and task hard limit. Known usage is retained without clamping when it exceeds
  a limit; Phase 3 remains the only durable budget ledger and settles the
  reported breach.

  Capability registration and authorization are separate checks. Exactly one
  active Tool is registered per capability, enabled adapter modes are
  validated at composition time, and the requested capability must occur
  exactly in `ResearchTask.required_capability_ids`. Registry existence never
  grants permission and there is no real-to-mock fallback. Mock Agent/Tool
  adapters reject descriptors claiming any mode other than `MOCK`. Phase 4
  does not carry a second `RunConfig`; Phase 2 remains responsible for proving
  required capabilities are allowed by the run.

  A Tool logical operation key hashes the Phase 3 task operation key, Agent
  step, capability ID, Tool ID, adapter ID, Tool operation version, and the
  canonical hash of persistence-safe input. Model-provided `tool_call_id` is
  only trace correlation and is excluded, so retrying the same task operation
  yields the same Tool key. Phase 4 adds no generic durable deduplication
  journal and no Tool retry loop. Automatic retry after a Tool has executed but
  its terminal trace append failed is permitted only when task idempotency,
  Tool idempotency, and the trace-failure policy all allow it. Actual known
  usage is preserved, side effects are not rolled back, and the failure uses a
  stable code. Unknown timeout/cancellation usage remains `UNKNOWN` for Phase 3
  conservative settlement.

  Local retrieval is deterministic BM25 over a validated, root-confined JSONL
  corpus. Its read, parse, tokenize, and rank work runs in a bounded read-only
  worker thread so the coordinator event loop can still observe cancellation
  and deadlines. Corpus bytes and chunk count are capped. Cancelling the outer
  invocation cannot forcibly stop a Python worker thread; the bounded,
  side-effect-free worker may finish in the background. Python execution uses
  an isolated-interpreter subprocess, concurrently drained stdout/stderr hard
  caps, bounded artifact count/size, disabled stdin, a sanitized minimal
  environment, cooperative cancellation with terminate/kill cleanup, safe
  relative inputs, and explicit atomic artifact publication. It uses argument
  vector execution and never a shell. Its AST import allowlist is solely a
  supported-code policy and accidental-misuse guard. A bounded Python
  subprocess is not a secure hostile-code sandbox: it cannot prevent arbitrary
  filesystem, network, or native-code behavior, and complete cleanup of all
  descendant processes is not guaranteed on every platform. Only trusted code
  is allowed; container or VM isolation is deferred to Phase 9.

  Once an Agent decision has been dispatched, timeout, cancellation, or an
  adapter exception without explicit no-usage proof produces `UNKNOWN` usage.
  Because the resource contract has one certainty for all dimensions, prior
  known amounts cannot be represented together with an uncertain in-flight
  call; the final amount is therefore absent and Phase 3 conservatively settles
  the reservation. Before an Agent observes a successful Tool result, the
  runner verifies output type, adapter identity where present, and artifact
  producer/operation provenance. It also measures the canonical validated
  result against an explicit observation byte cap and rejects oversize results
  without truncation. These post-dispatch failures preserve honest Tool usage
  and require both task and Tool idempotency for retryability.

  The Phase 3 backend validates both expected output identity and media type.
  Phase 4 does not claim that final `artifact_ids` exist in a complete artifact
  manifest; artifact-reference-to-final-manifest integrity is deferred until a
  later phase owns that manifest boundary.
- **Consequences:** Phase 3 can execute a typed Agent backend without knowing
  provider or Tool details, retries keep stable logical Tool identity, and
  offline behavior covers local retrieval, Python, browser, and search
  contracts without fabricated real integrations. Agent/Tool semantic traces
  are not placed in the Phase 3 checkpoint outbox; a terminal append failure is
  explicit but Phase 4 provides no cross-process exactly-once guarantee.
  Durable Tool deduplication, real LLM/browser/search providers, hostile-code
  isolation, Evidence Memory, claims, and synthesis remain later-phase work.

## ADR-0021: Separate stable evidence/claim identity from revisions and runtime occurrence

- **Status:** Accepted
- **Date:** 2026-08-28
- **Context:** Phase 5 must retain evidence produced by retryable Agent/Tool
  execution without making mutable content or attempt identity the entity key.
  It also needs recoverable local persistence while avoiding a database, WAL,
  event-sourcing architecture, or Phase 6 truth judgement.
- **Scope note:** This ADR intentionally consolidates the three Phase 5
  decisions originally planned as ADR-0021 (identity and revision), ADR-0022
  (snapshot persistence and crash boundaries), and ADR-0023 (Agent observation
  ingestion and trace ordering). They share the same canonical identity,
  immutable revision, and receipt-replay invariant; recording them together
  prevents incompatible partial decisions while the separate paragraphs below
  retain all three decision boundaries.
- **Decision:** A source is identified by run, source type, and canonical
  locator. Evidence is identified by run, source ID, immutable
  extractor-specific scope, and media type. Browser page bodies use the fixed
  `browser-page-body-v1` scope. Local chunks hash `local-chunk-v1`, document
  ID, and chunk ID. Search snippets hash `search-snippet-v1`, the safe canonical
  Tool-input hash, and a source-native snippet locator, with a canonical-source
  and normalized-snippet-hash fallback. Rank, attempt ID, attempt number, and
  model Tool-call ID never participate.

  `EvidenceRevision` represents changed content or extraction context.
  `IngestionReceipt` represents runtime occurrence provenance. The stable
  idempotency payload excludes occurrence fields. Retrying the same logical
  result with another attempt/Tool-call ID therefore reuses the evidence and
  writes a distinct receipt. Different result content changes the canonical
  request hash and ingestion operation identity; if source/scope are unchanged
  it creates an evidence revision rather than an idempotency conflict.

  `text-nfc-lines-v1` normalization applies Unicode NFC, normalizes CRLF/CR to
  LF, removes trailing spaces/tabs per line, and removes outer blank lines. It
  does not case-fold or collapse internal whitespace, and the original content
  remains immutable. Web source canonicalization lowercases scheme/host,
  removes fragments and default ports, and supplies `/` for an empty path. The
  raw query component is opaque: order, duplicates, and encoding are preserved.

  A claim ID hashes run and immutable `claim_scope_key`; normalized statement
  hash serves only content comparison and duplicate discovery. An edge ID
  hashes run, claim, and evidence. Exactly one current relation is permitted
  per claim/evidence pair, with relation changes represented as immutable edge
  revisions. Tombstones retain history. Conflict reporting nominates current
  support/contradiction sets but does not decide truth.

  Repeating `create_claim` for the same scope and normalized statement returns
  the current revision. Repeating it with different statement content appends
  the next revision to the same claim identity; explicit CAS-based
  `revise_claim` remains available. Generation context is revision provenance
  only. Before evidence ingestion, the Agent backend orders observations by
  Agent step and Tool-call ID so adapter output order cannot affect ingestion
  order.

  Phase 5 uses separate deterministic whole-file JSONL snapshots at
  `outputs/<run_id>/evidence.jsonl` and `claims.jsonl`. Each has a schema/store
  revision header, record count, canonical payload hash, and sorted records.
  Writes use a same-directory temporary file, file fsync, atomic replace, and
  parent-directory fsync where supported. Stores use compare-and-swap
  revisions, validate run/cross-record identity, and reject models requiring
  redaction. There is one application writer per run; this is not a WAL,
  transaction log, append log, or event source.

  Snapshot validation fails closed unless every entity revision chain is
  continuous from 1 through its maximum/current pointer, every predecessor and
  receipt target exists, edge claim-revision pins belong to the edge's claim,
  and mutation receipt/replay identities are unambiguous. Evidence references
  that require the separate Evidence Store remain joint service/validator
  checks rather than a fabricated cross-store transaction.

  Agent terminal results retain validated observations. The task backend
  extracts/ingests every eligible successful observation before mapping the
  Agent terminal result, even when that result is failure. Evidence/claim state
  is authoritative once the snapshot commits. A subsequent semantic trace
  append failure raises a typed `store_committed` failure, does not roll back
  state, and is safe to replay through receipts. Trace attributes contain only
  IDs, hashes, revisions, disposition/relation, and store revision.

  Citation integrity is structural. Dangling source/claim/evidence references,
  identity or content-hash mismatch, and nonexistent revisions are errors.
  Superseded but still addressable immutable revisions and tombstoned current
  entities are warnings and do not make the result invalid. Citation IDs are
  stable within a validation request; reusing one ID for a different reference
  is a structural error. Each citation also pins `source_id` and the expected
  evidence content hash, which must match the selected immutable evidence
  revision before a citation is structurally valid.
- **Consequences:** Task retries preserve logical evidence identity and honest
  runtime provenance, while claim and edge history remain stable under content
  changes. Local recovery can detect torn/corrupt snapshots without choosing a
  database. Multi-process leases, a general trace reconciliation outbox,
  evidence quality/truth scoring, synthesis, verification, and physical
  compaction are deferred to later phases.

## ADR-0022: Freeze inputs and publish deterministic bounded verification artifacts

- **Status:** Accepted
- **Date:** 2026-08-29
- **Context:** Phase 6 must synthesize and structurally verify Phase 5 claims and
  evidence without creating another Agent runtime, durable scheduler, budget
  ledger, or truth oracle. Model output is untrusted and publication must be
  replayable across a crash between the authoritative artifact and Markdown.
- **Decision:** A verification invocation freezes the Claim Graph and Evidence
  Memory snapshots exactly once. It computes their canonical hashes, derives a
  `verification_id`, and only then looks up the authoritative artifact. An
  existing artifact with the same identity is replayed without any model call.
  The exact verification identity hashes run ID, run revision, Claim snapshot
  hash, Evidence snapshot hash, policy hash, and model-bundle hash.
  `synthesis_id` hashes verification identity plus the `initial` discriminator;
  report-claim identity hashes synthesis, Claim ID, and Claim revision; citation
  identity hashes report Claim, Evidence ID/revision, source ID, and expected
  content hash. Finding and acquisition identities use immutable type/entity
  pins. Model prose, rationale, round, and attempt identity never participate.

  Claim and Evidence model context uses whole-item admission. If the complete
  record and current revision do not fit, the item is omitted and its ID is
  recorded; Phase 6 never truncates content while retaining its full-content
  identity or hash. Red findings always identify a report claim.
  `CONTRADICTORY_EVIDENCE` also identifies evidence, while `CITATION_GAP`
  cannot identify an existing citation. Every optional citation/evidence pin
  must belong to the frozen input.

  The Synthesizer creates exactly one `ReportClaim` for each selected claim.
  Structural support is derived from frozen current relations, never asserted
  by a model. Blue returns bounded actions, not a replacement document, and
  `REMOVE` changes a ReportClaim to `REMOVED` without deleting its identity.
  It also removes section membership and citation assignments. A claim is
  publishable only when it remains `INCLUDED`, Judge returns `SUPPORTED` or
  `QUALIFIED`, it has no unresolved blocking finding, and its citations have no
  structural error; `SUPPORTED` is legal only for
  `STRUCTURALLY_SUPPORTED`. Each bounded round is Red then Blue then Judge.
  Judge explicitly returns `FINALIZE` or `CONTINUE`, and continuation requires
  a matching unresolved finding or frozen structural conflict. Pending evidence
  acquisition alone cannot continue the frozen invocation. The maximum model
  call count is `1 + 3 * max_rounds`.

  Before publication, the service rechecks only the live stores' revision/hash
  fingerprint through a narrow metadata boundary. Any difference raises
  `verification_input_changed` and writes no artifact. Citation validation and
  conflict derivation use only the frozen snapshot-backed stores.

  Phase 6 first renders deterministic UTF-8/LF Markdown bytes and hashes them.
  It then constructs and atomically writes authoritative `verification.json`,
  followed by the already-rendered `report.md`. A crash after authority commit
  is reconciled idempotently from the stored artifact. Replacing a different
  verification identity requires explicit compare-and-swap intent. This is not
  a WAL or round checkpoint.

  Phase 6 reports structural verification and evidence-supported judgement,
  not factual truth. It accepts an already-`VERIFYING` Run and reports actual
  usage plus certainty. Phase 3 remains the sole owner of durable execution,
  retry, checkpoints, and budget settlement; durable lifecycle integration is
  deferred.

  Phase 6 has exactly one current-valid edge derivation. It admits an ACTIVE
  edge only when its current edge revision exists, both referenced entities are
  ACTIVE, and the edge revision pins both entities' current revisions. A stale
  pin is ignored rather than upgraded; a dangling entity or revision is typed
  input corruption. Evidence selection, citation allowlists and assignments,
  structural support, conflicts, and relation lookup all consume that same
  derived set. Evidence ordering is CONTRADICTS, SUPPORTS, CONTEXTUALIZES, then
  edge ID and Evidence ID.

  Blue acts exactly once on each open finding and cannot resolve it. Multiple
  findings may address one Claim; only incompatible mutations conflict. Judge
  decides every ReportClaim and dispositions every open finding exactly once.
  Re-emitting a stable finding ID reopens it for the current round; the ID is
  permanent identity and cumulative quota deduplication, not permanent
  resolution. UNKNOWN Judge usage with FINALIZE remains publishable as UNKNOWN,
  while UNKNOWN with CONTINUE stops before another model call. All invocation
  usage, certainty, expected mode, and trace callback state is local, making a
  coordinator reentrant across concurrent runs.

  A `CitationAssignment` binds each final citation to a current-valid edge and
  relation. SUPPORTED and publishable QUALIFIED Claims require a SUPPORTS
  assignment. A conflicted Claim published QUALIFIED must also disclose a
  CONTRADICTS assignment; CONTEXTUALIZES alone is never support. Draft revision
  identity additionally binds the parent revision ID. Model prose and titles
  are escaped as plain text; only the deterministic renderer owns Markdown
  structure and citation footnote syntax.

  The authoritative result stores both store revisions, all selected/omitted
  IDs, complete draft lineage, validated Red/Blue/Judge round records, finding
  dispositions, acquisition requests, citation issues/assignments, termination
  reason, usage certainty, Markdown hash, and supersession identity. A
  canonical content hash excluding itself detects silent authority tampering.
  Same-ID publication compares the entire canonical result before writing the
  derived report. Persistence failures expose `authority_committed`,
  `report_committed`, and `artifact_committed`; completion-trace failure after
  publication is separately typed with `artifact_committed=True`. Expected
  prior authority absence fails before model calls. This remains atomic
  snapshot publication and reconciliation, not a WAL or checkpoint system.
- **Consequences:** Offline exact fixtures can reproduce a complete
  synthesis/verification result, replay avoids provider calls, and publication
  has one clear authority. New evidence acquisition is represented as a
  request artifact only; it is not executed inside the frozen invocation.

## ADR-0023: Evaluate immutable artifact snapshots with typed replayable metrics

- **Status:** Accepted
- **Date:** 2026-08-30
- **Context:** Phase 7 must compare planning, execution, evidence, citation,
  and report behavior without mutating Runs, inventing unavailable values, or
  treating an optional model judge as ground truth. Existing artifacts do not
  include a unified manifest, invalid planning candidates, ranked retrieval
  occurrences, or a fully verifiable SUT configuration bundle.
- **Decision:** Evaluation freezes and hashes the existing Phase 2-6 artifacts
  through a read-only boundary, validates every case/run binding before any
  evaluator call, and stores results under a standalone evaluation root.
  Reference annotations affect metrics only; only explicit required execution
  conditions participate in compatibility. Source policy requirements use the
  distinct `UNSPECIFIED`, `REQUIRE_NONE`, and `REQUIRE_EXACT` states.

  A single EvaluationRun requires homogeneous observable SUT pins. Verified or
  declared value conflicts fail; unobservable pins only downgrade provenance;
  design absence is never encoded as a value. Comparison and ablation inherit
  the weakest provenance, and an unverified configuration declaration cannot
  be presented as a verified causal experiment.

  Metric results are strict typed values with explicit computed,
  not-applicable, skipped, unavailable, and error states. The authority stores
  the exact metric definition snapshots. Evaluator bundle identity binds
  evaluator versions, definition hashes, normalization versions, and
  aggregation semantics. Deterministic evaluators independently recompute DAG,
  current-edge, citation, and publication correctness from the lowest
  authoritative snapshots instead of trusting reported conclusions.

  When the optional judge is enabled, `evaluation_model_bundle_hash` is stored
  in the authority and participates in both evaluator-bundle and EvaluationRun
  identity, so a loader can verify that pin without external declarations.

  Invocation policy bounds cases, metrics, artifact bytes, model context and
  response bytes, duration, and the global model call count. Model evaluation
  is optional, provider-independent, MOCK-only in Phase 7, exact-fixture, and
  has no retry or fallback. Cancellation or deadline expiration publishes no
  authority.

  Semantic hashes exclude timestamps and persistence occurrence metadata;
  physical artifact hashes cover the complete persisted artifact. Same-input
  authority replay makes zero evaluator/model calls. Case artifacts commit
  before the authoritative evaluation snapshot; this is not a WAL, checkpoint,
  event source, or second runtime.

  Freezing performs one bounded raw read per artifact and parses those exact
  bytes. `PRESENT` means typed parsing succeeded; malformed bytes remain
  addressable as `CORRUPT` with their original hash, size, and a sanitized
  reason. Corruption fails only its Case when isolation is possible. All failed
  Cases make the EvaluationRun failed; mixed failed/partial/completed Cases make
  it partial. The final live fingerprint check is immediately adjacent to
  authority publication.

  SUT request pins are declarations only. Artifact verification requires an
  artifact reference and byte hash; absent-by-design requires an explicit arm
  declaration plus an actually absent artifact. Commit and system version are
  declared identity pins, so they prevent an artifact-verified overall grade.
  Pin-version conflict is a homogeneity error. A verified ablation intervention
  may be either an artifact-verified value change or an explicit absence whose
  artifact is confirmed absent; unchanged controls must be artifact-verifiable
  or identically proven absent.

  Numeric metrics, aggregates, and deltas use
  `normalize_metric_decimal_v1`: finite Decimal arithmetic, twelve decimal
  places, and `ROUND_HALF_EVEN`. Relative thresholds apply symmetrically to
  regressions and improvements; `NO_DECREASE` has zero tolerance. `TARGET` and
  informational metrics are not valid Phase 7 regression rules.

  Evaluation, comparison, and ablation authorities use immutable same-ID
  first-writer creation. Identical bytes replay idempotently; different bytes
  raise a typed conflict, including under concurrent publication. Ablation
  authority binds and validates the complete canonical `AblationSpec`.
- **Consequences:** Offline comparisons remain reproducible and explainable,
  missing data cannot become a favorable zero, and ablation claims retain
  honest provenance. Retrieval ranking metrics, invalid-plan history, real
  judges, execution orchestration, statistical release gates, and evaluation
  trace exporters remain deferred.

## ADR-0024: Use a bounded verification operation journal and local-first observations

- **Status:** Accepted
- **Date:** 2026-08-30
- **Context:** Phase 6 publishes a deterministic verification authority but an
  interruption between model dispatches, response validation, publication,
  trace append, and Run lifecycle transitions cannot yet be reconciled without
  repeating an uncertain provider call. Optional exporters must not weaken
  local durability or consume the business deadline.
- **Decision:** Add one bounded
  `verification_operations/<verification_id>.json` snapshot per verification
  generation. `DurableVerificationCoordinator` owns lifecycle only,
  `VerificationOperationManager` owns checkpoint/CAS/call-journal/outbox,
  existing `VerificationService` retains all Phase 6 correctness, and
  `ObservationRecorder` owns diagnostics and export offering. The operation is
  not a second result authority, runtime, Run state, or trace.

  Every model call is durably PREPARED and DISPATCHED before provider work.
  Preparation pins stable stage/call identity, bundle, request/response schema,
  safe structured input identity, canonical request hash, and predecessor
  response hashes. Recovery reconstructs the canonical request and validates
  every pin before replaying a RESPONSE_COMMITTED typed payload. Raw prompts,
  provider requests, and raw responses are not persisted. A DISPATCHED call
  without a committed validated response becomes INTERRUPTED_UNKNOWN with
  UNKNOWN usage and receives no transparent retry. Existing Phase 6 semantics,
  not the durability layer, decide all subsequent business behavior.

  The operation immutably pins invocation hard limits and, at
  READY_TO_PUBLISH, the complete validated `VerificationResult`, exact rendered
  Markdown text/bytes, both content hashes, predecessor authority, and stable
  publication key. Recovery publishes that exact occurrence rather than
  rebuilding timestamped output. Multiple generations for one Run coexist so
  an explicitly declared prior authority can be superseded without an old
  completed operation blocking the new verification.

  Filesystem updates use a shared per-operation in-process lock. Inside the
  lock the adapter reloads current revision, validates expected N and proposed
  N+1, then atomically replaces and fsyncs the snapshot. Observation delivery
  reuses immutable trace descriptors and idempotent append: equal ID/hash is a
  no-op, conflicting content is corruption. Checkpoints have a configurable
  default byte bound and an absolute 64 MiB ceiling, enforced before replace
  and during load in memory and filesystem adapters. Local append-once is
  synchronous but delivery failure never reverses a committed business mutation;
  optional exporter delivery is a nonblocking `put_nowait` into an in-process,
  bounded, best-effort dispatcher. Remote work is never awaited by business
  orchestration. Export failure/drop diagnostics are local-only, include the
  exporter identity, cannot recurse, and execute through a bounded daemon-thread
  diagnostic queue rather than the asyncio business loop. Queue-full and
  dispatcher-stopped diagnostics use that same off-loop boundary; if its bounded
  queue is full, the diagnostic may be dropped because the original event is
  already locally durable.

  Critical outbox capacity is proved before entering VERIFYING using
  `12 + 3*(1 + 3*max_rounds) + 2*16`. Phase 6's maximum is 137 descriptors,
  within 160 reserved critical slots and 256 total slots. An invalid future
  bound fails preflight. Recovery attempts 1..16 are durable CAS mutations;
  attempt 17 raises a typed limit error without mutation or business action.
  Recovery STARTED is checkpointed at entry; COMPLETED is emitted only after
  actual reconciliation. Known failed calls terminalize the operation as
  FAILED, explicit cancellation as CANCELLED, and unknowable in-flight outcomes
  as INTERRUPTED_UNKNOWN.

  Before `RUNNING -> VERIFYING`, lifecycle orchestration loads and reconciles
  the existing Phase 3 checkpoint, matches its Run/revision and embedded DAG
  identity, requires `ExecutorStatus.COMPLETED`, and rejects unsafe outstanding
  attempts, retries, replans, cancellation, reservations, or uncertain
  consumption. Failure creates no verification operation and makes no model
  call. Provider/client exceptions after durable DISPATCHED are
  INTERRUPTED_UNKNOWN unless no external execution is provable. Deterministic
  predispatch deadline/request failures terminalize once as FAILED, and
  explicit predispatch cancellation once as CANCELLED.

  Operation storage exposes bounded per-Run generation discovery. A new target
  cannot bypass an unfinished, unknown, authority-less failed/cancelled, or
  authority-less publication-ready generation after mutable inputs change.
  Creation is legitimate only after the exact current authority has a matching
  completed operation and is explicitly pinned as predecessor. Matching
  completed-authority recovery may settle pending local outbox entries best
  effort, but performs no model work, lifecycle mutation, or result mutation.

  A legacy authority is validated from its own identity, integrity, and pins
  before bootstrapping an operation. Current Claim/Evidence state is only a
  consistency check and cannot redefine historical authority. Authority
  precedence never overrides a mismatch. Lineage is independently rebuilt
  from citations, current-valid SUPPORTS edges, exact evidence revision/content
  and source pins, receipts, and runtime provenance;
  persisted citation assignments are diagnostics only.
- **Consequences:** Committed responses and authorities replay without provider
  calls, uncertain dispatches are not duplicated, concurrent in-process writers
  have one CAS winner, path-lock registry entries are released after use, and
  local observations survive exporter failure. Matching authorities on Runs
  already beyond EVALUATING replay read-only; optional outbox omission uses its
  own semantic event rather than remote-delivery-drop terminology. Durable
  remote delivery, exporter retry, cross-process leases, real integrations, and
  resolution of unknown provider outcomes remain Phase 9 work.

## ADR-0025: Freeze REAL provider composition before lifecycle mutation

- **Status:** Accepted
- **Date:** 2026-08-31
- **Context:** Phase 9A introduces real LLM calls. Resume and mutation cannot
  safely reuse a Run if provider request semantics, byte/deadline bounds,
  pricing rules, prompts, endpoints, or adapters changed. Credentials must
  remain rotatable and outside durable artifacts. Atomic replace alone also
  cannot provide immutable first-writer behavior between adapter instances.
- **Decision:** Persist `outputs/<run_id>/real_composition.json` as a bounded
  `RealCompositionEnvelope`. Its one-time `recorded_at` is occurrence metadata;
  identity is the non-circular chain `ModelCallPolicySnapshot ->`
  `ModelBundlePayload -> SemanticCompositionPayload -> composition_hash ->`
  `composition_id`. Every load independently recomputes nested hashes, the
  composition ID, and the envelope content hash. Provider/profile/model,
  canonical base endpoint including path, prompt/response contracts,
  generation controls, request/response limits, four transport timeouts,
  currency, cost ceiling, and versioned per-model pricing upper-bound rules are
  resume-frozen. An adapter-owned, model-specific pricing safety profile pins
  conservative rate floors, USD billing currency, a 1,000,000-token context
  window, and a 384,000-token provider output maximum. Without a proven local
  tokenizer, the full context window supplies the input reservation;
  `max_input_tokens` remains post-response integrity only. Its version/hash and
  the derived provider-call token/cost reservation are part of policy, bundle,
  and composition identity. Operator rates may be raised but cannot undercut
  those floors; non-USD currency and unknown models fail closed. The profile is
  reviewed versioned safety data, not a permanent provider-price guarantee.
  Credential values are excluded, but required presence is revalidated. The
  safe credential-slot ID is resume-frozen inside each model bundle.

  Filesystem create uses a process-wide normalized-path lock shared across
  adapter instances. Existence reload, validation, semantic comparison, and
  first write occur inside that lock; equal semantics return the original
  envelope, while different semantics conflict without overwrite. Atomic
  replace supplies crash-safe publication, not CAS by itself.

  `RunManager` keeps its existing default REAL rejection. An explicitly
  injected `RunIntegrationGuard` validates REAL create/resume and
  `_load_for_mutation()` validates the authority after lifecycle trace
  reconciliation for every transition/finalize path. `CREATED -> PLANNING`
  additionally requires a process-local binding. Real adapters can be built
  only through that binding. A read-only dispatch authorizer checks
  role-specific Run status at construction and immediately before each call.
  The immediate pre-dispatch check also reloads and validates the durable
  composition authority, recomputes current semantic composition, and matches
  the process-local binding hash. Read-only `load()` remains read-only.

  DeepSeek implements the existing PlanningModel, Agent, and VerificationModel
  ports through one raw HTTPX OpenAI-compatible transport. Calls are bounded,
  non-streaming, structured JSON, explicit identity, and have no retry or
  fallback. DeepSeek is HTTPS-only; child tasks are cancelled and joined on
  parent cancellation; raw provider exceptions do not cross the stable error
  boundary. Phase 9A explicitly sends versioned thinking mode and reasoning
  effort. Thinking-enabled roles canonically omit ignored temperature/top-p;
  thinking-disabled roles require them. Only `finish_reason=stop` is success;
  all other reasons fail with stable response-received codes and preserve any
  validated usage without adapter retry. Provider token counts must fit explicit
  frozen input/output caps. `max_input_tokens` is post-response integrity only;
  neither HTTP bytes nor local tokenization establish admission. A separate
  adapter-owned provider/model upper bound supplies pre-dispatch token/cost
  reservation, which must fit the Run budget and Agent task hard limit through
  existing owners and never creates a second ledger. Local cost charges all
  input tokens at a pinned
  per-model maximum rate, is always `UPPER_BOUND`, and never uses HTTP bytes as
  tokenizer truth or labels locally inferred billing `EXACT`. Phase 8
  still marks any provider exception after its durable
  DISPATCHED record as `INTERRUPTED_UNKNOWN`; Phase 9 does not reinterpret it.

  DeepSeek-specific safety policy remains in the Phase 9 domain for 9A and may
  later become a generic provider pin. Composition pins requested model aliases,
  not immutable physical weights. Provider `system_fingerprint` is deferred as
  safe E2E/evaluation provenance and is not recovery authority.
  Official-host allowlisting remains deferred to Phase 9D security hardening.

  Phase 9A fails closed when REAL capabilities are non-empty and exposes no
  generic Tool union to its Agent model. Exact capability schema binding is a
  Phase 9B prerequisite; Phase 4 AgentRunner remains final authorization owner.
- **Consequences:** An existing REAL Run fails closed on missing, tampered, or
  changed composition while credential rotation remains possible. Current
  first-writer guarantees are in-process; deployment permits one writer
  process per Run. Tavily, Browser, embeddings, Milvus, Langfuse, automatic
  Claim Extraction, workflow sequencing, and real E2E remain Phase 9B-9E.

## ADR-0026: Bind REAL web capabilities to immutable policy and existing runtime authority

- **Status:** Accepted
- **Date:** 2026-09-02
- **Context:** Phase 9B enables model-selected Search and Browser operations.
  Registry presence is not permission, a model-authored call ID is not durable
  Tool identity, provider metadata is not source evidence, and URL validation
  without connection pinning does not close DNS rebinding.
- **Decision:** Freeze each REAL capability's descriptor, adapter and operation
  versions, exact policy hash, and provider-reservation hash in the existing
  REAL composition. Keep `ToolInvocationRequest` v1 unchanged; `AgentRunner`
  constructs an ephemeral `AuthorizedToolDispatchEnvelope` containing the full
  immutable task contract and its hash. A narrow REAL Tool port accepts only
  that envelope. Immediately before secret access or DNS, the authorizer
  reloads Run/composition authority, requires `RUNNING`, checks task and
  capability permission, registry binding, descriptor, operation version,
  policy, reservation, and currency. The existing AgentRunner usage
  accumulator performs remaining-limit admission before every provider-backed
  Agent decision and Tool dispatch. Adapters do not retry; a retryable Tool
  failure terminates the current attempt so Phase 3 remains retry owner.

  Tavily uses only the frozen STANDARD `POST https://api.tavily.com/search`
  profile, explicit documented fields, no redirects, environment proxy, or
  retry, and a versioned conservative one-credit USD upper-bound reservation.
  Provider summaries are `SearchResultV2` metadata and produce no Evidence.
  Existing SearchResult v1 behavior and extraction remain unchanged.

  Capability-empty Phase 9A Runs retain their direct-only Agent prompt/schema
  and `agent-direct-decision-v1` identity. Enabling Search or Browser selects
  `agent-tool-decision-v2`; its prompt/schema hash flows through the Agent
  model bundle and REAL composition, so resume never reinterprets old authority.

  The Browser freezes exact HTTP(S)/port tuples; a ResearchOS-owned special-use
  address table/version/hash plus IPv4-mapped and mixed-DNS policy;
  DNS/rebinding, redirect, ASCII canonical-target reject-non-ASCII, extraction,
  and NFC/newline normalization versions; MIME/charset/identity-encoding
  allowlists; User-Agent; and relevant URL/body/time bounds in its composition
  policy. The custom pinned asyncio HTTP/1.1 transport consumes these pins,
  rejects ambiguous whitespace/Unicode targets, requires two identical bounded
  DNS answer sets per hop, connects to a selected numeric address, verifies the
  connected peer, preserves hostname TLS SNI/certificate validation, and handles
  every redirect as a fresh authorization hop. Its frozen total timeout bounds
  the full invocation and equals the provider reservation duration. This is an
  SSRF control for the supported HTTP Browser, not a hostile-code sandbox.

  A known terminal Search/Browser result consumes one logical Tool call once
  invocation began, independent of provider HTTP dispatch; reservation denial
  before invocation consumes zero. Tavily uses a request-scoped HTTPX client,
  so cached per-Run registry bindings do not retain unclosed clients.

  Phase 9B introduces explicit application-only admission and retry-delegation
  markers. The capability-empty Phase 9A direct Agent profile preserves its
  original per-call token/cost task-limit preflight and does not use the Phase
  9B accumulated remaining-budget admission. Only the versioned web-tool Agent
  profile uses that new admission. Only Tavily and Browser explicitly delegate
  retryable infrastructure failures to Phase 3; historical Tool retryability
  retains its Agent observation semantics. Agent HTTP operations remain bounded
  end-to-end by the minimum of the frozen web Agent total-call timeout and the
  absolute task deadline. HTTPX phase timeouts remain stall bounds only.
  Factory registry caching releases terminal Runs on the next registry access
  and also has an explicit `evict_capability_registry` process-local lifecycle
  boundary. Unknown Tool dispatch/cancellation outcomes fail closed; their
  monetary usage is never silently treated as zero.
- **Consequences:** REAL Search/Browser resume only under identical immutable
  semantics and cannot bypass existing lifecycle, authorization, retry, or
  budget ownership. Tavily summaries can guide navigation but cannot silently
  become evidence; validated Browser bodies may enter Phase 5 normally.
  Cross-process Tool dedupe, JavaScript browsing, retrieval providers, real E2E,
  and broader network sandboxing remain later Phase 9 work.

## ADR-0027: Restrict REAL retrieval to an attested Zilliz Cloud Free-plan BM25 profile

- **Status:** Accepted
- **Date:** 2026-09-04
- **Decision:** Phase 9C supports one read-only `managed_retrieval` capability:
  an operator-attested Zilliz Cloud Free-plan serving-endpoint profile with Milvus built-in
  BM25. Its canonical endpoint is HTTPS, no explicit port, and exactly
  `<cluster>.serverless.<region>.vectordb.zillizcloud.com`; authorization and
  composition validation precede endpoint validation, which precedes secret
  lookup and the sole search RPC. Private/global/on-demand/Dedicated/arbitrary
  Milvus endpoints fail closed. The endpoint family is shared by Free and paid
  Serverless deployments and therefore never proves a Free plan. Zero-cost
  admission requires a frozen operator-provisioned Free-plan attestation; that
  attestation participates in the retrieval policy and REAL composition hash.

  Only an attested Free plan has a fixed zero monetary upper bound, so its
  immutable reservation is zero USD cost, one Tool call and the policy total
  timeout. Variable billed profiles cannot prove a per-call upper bound and are not
  supported. This is admission metadata only; Phase 3 remains the budget and
  retry owner. The frozen collection contract includes `document_id`,
  `chunk_id`, `locator`, `content`, stored `content_hash`, and bounded safe
  metadata. Each returned content value is re-hashed from original UTF-8 bytes;
  mismatch is `retrieval_response_invalid` and produces no Evidence.

  The adapter has no describe/list/schema-inspection call, collection mutation,
  embedding, dense/hybrid path, reranker or fallback. It issues one bounded
  search RPC, delegates retryable infrastructure failure to Phase 3 and retains
  UNKNOWN usage on interrupted dispatched work. Retrieval-enabled REAL Agents
  use a distinct v3 Tool response contract; Phase 9A direct v1 and Phase 9B
  web-only v2 contracts remain unchanged.

## ADR-0028: Mirror local trace to optional OTLP without changing authority

- **Status:** Accepted
- **Date:** 2026-09-04
- **Decision:** Phase 9D keeps canonical local `TraceSink` persistence as the
  sole trace authority. A synchronous decorator writes locally first and offers
  only newly appended descriptors to a bounded, thread-safe dispatcher. The
  dispatcher owns background exporter execution when enabled; disabled mode
  owns no worker. Equal `append_once` replays, reconciliation, and local-only
  diagnostics never re-export. A fixed projection maps only allowlisted event
  fields and identities to an `ObservationEnvelope`.

  The initial optional backend is OTLP/HTTP protobuf using existing `httpx`
  and `opentelemetry-proto`, no vendor SDK. It uses direct HTTPS, disabled
  redirects/environment proxies, bounded time/bytes, and secret-only safe auth
  headers. Remote failure, timeout, backpressure, and shutdown loss are
  best-effort diagnostics only and do not affect Run lifecycle, budget, retry,
  checkpoint, Evidence, or Verification semantics.
