# Phase 8 Design - Observability and Durable Verification Integration

Phase 8 makes the existing Phase 6 verification invocation recoverable and
adds local-first structured observations. It does not add another scheduler,
verification algorithm, result authority, Run state model, or trace system.

## Ownership

- `DurableVerificationCoordinator` owns only Run lifecycle orchestration.
- `VerificationOperationManager` owns the operation checkpoint, compare-and-
  swap mutations, prepared model-call journal, response replay, and bounded
  local observation outbox.
- `VerificationService` remains the sole owner of synthesis, Red/Blue/Judge
  correctness, citation validation, disposition, rendering, and authoritative
  `VerificationResult` publication.
- `ObservationRecorder` performs durable local append-once recording and then
  offers optional remote delivery without waiting for it.

Authority precedence during recovery is `VerificationResult`, `RunState`,
the target `verification_operations/<verification_id>.json`, then local trace.
Precedence never overrides an identity, hash, or pin mismatch; every mismatch
fails closed.

## Durable verification operation

The operation identity is a stable hash of operation schema/version and the
Phase 6 `verification_id`. Its checkpoint records Run and verification pins,
the exact `VerificationPolicy`, immutable hard limits, monotonically increasing
revision, operation status, model-call records, recovery attempts, and
immutable observation descriptors. It is a bounded snapshot and call journal,
not a WAL or a second `VerificationResult` authority.

READY_TO_PUBLISH additionally freezes one complete validated result occurrence,
exact UTF-8 rendered Markdown, artifact and Markdown hashes, predecessor
authority, and stable publication key. Recovery publishes these exact bytes;
it never recreates `completed_at` or another occurrence. Generations coexist by
verification ID, but a new generation cannot bypass another unfinished or
authority-less terminal generation. Supersession requires a completed prior
generation whose exact result is current authority and is explicitly pinned as
the predecessor.

Model calls progress exactly through `PREPARED`, `DISPATCHED`, and
`RESPONSE_COMMITTED`. A `PreparedModelCall` stores its stable stage and call
keys, model bundle hash, canonical structured-request hash, safe request pins,
request schema and response contract versions, and predecessor response
hashes. It never stores raw prompts, provider requests, or raw provider
responses. A committed response stores only the validated typed payload,
identity metadata, usage/certainty, and canonical payload hash.

Before a provider dispatch the PREPARED and DISPATCHED mutations are durable.
On replay, Phase 6 reconstructs the request from the frozen input and committed
typed predecessors. The manager recomputes and verifies request hash, stage
key, call key, bundle, contract, and predecessor hashes before returning the
committed payload. A dispatched call without a durable validated response is
`INTERRUPTED_UNKNOWN`, has `UsageCertainty.UNKNOWN`, and is never transparently
retried. Transport/client exceptions after durable dispatch have this same
unknown outcome unless no provider execution can be proved. A deadline,
request/context bound, deterministic request failure, or cancellation known
before dispatch terminalizes the operation once as FAILED or CANCELLED. Phase 8
does not infer Red/Blue/Judge continuation, disposition, or a
terminal Run result from that operational fact.

## CAS and recovery

The filesystem operation store uses a process-wide per-operation lock shared
by adapter instances. While holding it, save reloads the current file, requires
`expected_revision == current.revision` and `proposed.revision == N + 1`, then
uses same-directory temp write, file fsync, atomic replace, and parent fsync.
Two writers that read revision N cannot both commit N+1. Lock registry entries
are reference-counted and removed after the last user. Serialized checkpoints
are checked before replace and on load with an absolute 64 MiB ceiling; typed
response payloads also have a cumulative hard cap. Cross-process leases remain
deferred.

Recovery attempts 1 through 16 are accepted through CAS. Attempt 17 raises
`RecoveryAttemptLimitExceeded` before checkpoint mutation, descriptor
allocation, model calls, authority publication, lifecycle changes, or a
disposition decision. Replaying a completed operation consumes no attempt.

An existing valid Phase 6 authority with no operation is recovered by first
validating the authority and its own audit pins, constructing a bootstrap
operation from those historical pins, and using the current Claim/Evidence
freeze only as a consistency check. Mutable current state never redefines a
historical verification identity. Any mismatch is a typed recovery
inconsistency.

## Observation durability and export

An observation contains a full immutable Phase 3 `TraceEventDescriptor` plus
typed correlation pins. Local `append_once` is the only synchronous recording
step:

- same event ID and canonical hash is `ALREADY_PRESENT`;
- same event ID with another hash is corruption;
- a missing event is appended, flushed, and fsynced.

Consequently, a crash after trace append but before the checkpoint records the
descriptor as delivered cannot create a duplicate physical JSONL event.
Persisted trace remains the one local trace; the envelope is not another log.

Operation/outbox commit is the business boundary: trace delivery failure or a
delivered-flag CAS conflict leaves the descriptor pending and never fails the
business mutation.

After local acceptance, `ExporterDispatcher.offer` performs `put_nowait` and
returns without network I/O. The in-process, non-durable dispatcher has 256
pending slots, concurrency 4, and a two-second independent timeout. Delivery
failure, timeout, shutdown, or backpressure cannot change verification output,
Run state, usage, business deadline, or lifecycle progress. Queue overflow
uses deterministic `DROP_NEW`; the original local event remains durable and a
bounded local-only `observability.delivery_dropped` diagnostic is written.
`observability.export_failed` and delivery diagnostics have `export=false` and
never re-enter the dispatcher. Export-failure and queue-full/stopped diagnostics
run through the same bounded daemon-thread queue, so filesystem fsync cannot
block the asyncio business loop. A full diagnostic queue deterministically
drops its diagnostic because the original observation is already durable.
Pending exports/diagnostics may be lost at
process exit. Optional outbox omission uses
`observability.optional_omitted`, not remote-delivery-drop semantics.

## Capacity proof

The persisted policy validates before entering `VERIFYING`:

```text
max_model_calls = 1 + 3 * max_rounds
max_critical_descriptors =
    12
  + 3 * max_model_calls
  + 2 * max_recovery_attempts
```

With Phase 6 `max_rounds <= 10`, `max_model_calls <= 31` and the critical
maximum is `12 + 3*31 + 2*16 = 137`. The checkpoint reserves 160 critical
slots inside 256 total slots. Both `max_critical_descriptors <= 160` and
`160 < 256` are mandatory preflight invariants. A future policy that violates
them is rejected before the Run enters `VERIFYING`. Optional descriptors use a
stable order and reserve one optional slot for an omitted-count/hash summary;
critical recovery events are never displaced.

## Lineage and lifecycle

Verification lineage is reconstructed from final draft citations,
independently derived current-valid SUPPORTS edges, exact evidence revision
content/source pins and ingestion receipts, and task/attempt/tool provenance
when present. Persisted citation assignments are diagnostic comparisons only.
Stale, contradictory, dangling, or unreceipted pins never establish lineage.

The lifecycle path is `RUNNING -> VERIFYING -> durable operation -> valid
VerificationResult authority -> EVALUATING`. Phase 8 never runs Phase 7
evaluation. Run lifecycle mutations continue through `RunManager` and its
existing intent-state-committed protocol. Before `RUNNING -> VERIFYING`, the
coordinator uses the existing Phase 3 `CheckpointManager` to reconcile the
checkpoint, requires matching Run/revision and DAG identity plus
`ExecutorStatus.COMPLETED`, and rejects outstanding running/retry/interrupted or
unknown attempts, replan/cancellation state, reservations, and uncertain usage.
A failed gate performs no transition, operation creation, or model call.

Known model-call failures terminalize the operation as FAILED, explicit
cancellation as CANCELLED, and unknowable dispatched outcomes as
INTERRUPTED_UNKNOWN. Recovery STARTED and COMPLETED are separate mutations;
COMPLETED is recorded only after reconciliation. A matching authority on a Run
already in EVALUATING or a terminal state is an idempotent read/reconcile path.
Its pending local outbox may be settled best effort without a model call,
lifecycle transition, or business-result mutation. Cancellation terminals emit
cancellation events; timeout/transport unknowns emit interrupted-unknown; known
failures emit verification-failed.

## Explicit deferrals

Real providers/exporters, durable remote export queues, exporter retry,
cross-process leases, distributed operation ownership, Phase 7 execution, and
operator-controlled uncertain provider reconciliation remain Phase 9 work.
