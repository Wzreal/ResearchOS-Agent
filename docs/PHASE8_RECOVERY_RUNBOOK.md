# Phase 8 Verification Recovery Runbook

This runbook covers local Phase 8 verification recovery. It does not authorize
manual JSON edits, provider retries, or Run-state guesses.

## Authority order

Inspect artifacts in this order:

1. `verification.json` and its deterministic `report.md` projection;
2. `run_state.json`;
3. `verification_operations/<verification_id>.json`;
4. `trace.jsonl`.

Higher precedence never permits an identity mismatch. Validate Run identity,
verification identity, policy/model/input pins, canonical content hashes, and
schema versions before reconciliation.

## Expected recovery cases

- A complete operation and valid authority replay without consuming a recovery
  attempt or calling a model.
- A valid authority with no operation is bootstrapped only after validating the
  authority from its own audit fields. Current Claim/Evidence snapshots are a
  consistency check, not a source for redefining historical identity.
- A committed typed model response is replayed only after reconstructing its
  structured request and matching request, stage, call, bundle, contract, and
  predecessor hashes.
- A PREPARED call may be dispatched because provider work was not recorded as
  started.
- A DISPATCHED call without a durable validated response becomes
  `INTERRUPTED_UNKNOWN`. Do not retry it automatically or invent usage.
- A local trace descriptor whose event ID and hash already exist is settled as
  delivered without another physical append.
- READY_TO_PUBLISH reuses the complete persisted result and exact Markdown;
  never rebuild a new completion occurrence after restart.
- Incoming hard limits must hash exactly to the operation pin before replay or
  model dispatch.
- An older authority is superseded only when it is the exact persisted/declared
  predecessor and its generation is completed. An unfinished or authority-less
  generation must be resolved before mutable input/policy changes can create a
  new target.
- A matching authority with an EVALUATING or terminal Run is reconciled without
  another model call or lifecycle transition. Pending local outbox descriptors
  may be settled best effort without changing the business result.
- A RUNNING Run enters VERIFYING only after the existing Phase 3 checkpoint is
  reconciled, matches Run/revision/DAG identity, is executor COMPLETED, and has
  no unsafe outstanding execution or budget state.

## Fail-closed cases

Stop and preserve all files when any schema, hash, Run identity, verification
identity, policy/model/input pin, response proof, authority/checkpoint pairing,
or same-event-ID content differs. These are recovery inconsistencies or
corruption, not repair hints.

Also stop on a missing/nonterminal/incompatible Phase 3 checkpoint, an
unresolved older verification generation, or an unknown dispatched provider
outcome. A provider transport exception after durable DISPATCHED is
`INTERRUPTED_UNKNOWN`, never ordinary FAILED or an automatic retry.
Deterministic predispatch deadline/bound failures and explicit predispatch
cancellation are durable one-time FAILED/CANCELLED terminals.

The seventeenth recovery attempt raises `RecoveryAttemptLimitExceeded` before
any mutation or model/business action. Operator intervention is required.

## Exporter diagnostics

Remote export is best effort and non-durable. `observability.export_failed` and
`observability.delivery_dropped` are local-only diagnostics and never re-enter
the exporter. All diagnostic local filesystem writes, including queue-full and
dispatcher-stopped drops, run on a bounded daemon diagnostic worker, not the
asyncio business loop. A full diagnostic queue may drop the diagnostic because
the original event is already durable. Missing remote observations
after process exit do not invalidate
the local trace or verification authority. Do not delay lifecycle recovery to
drain exporter workers.

## Deferred operator actions

Cross-process lease repair, reconciliation of provider-side unknown billing or
side effects, durable remote delivery, and real-provider investigation are
Phase 9 operational concerns. Phase 8 provides no command that guesses those
outcomes.
