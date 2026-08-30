# Phase 7 Test Coverage Audit

Phase 7 has 49 focused tests. The final complete Phase 1-7 regression run on
2026-08-30 collected and passed 626 tests.

## Contract and compatibility coverage

- strict typed metrics, unavailable versus zero, zero-denominator N/A;
- source-policy three-state requirements and exact RunInput hash reuse;
- reference annotations excluded from preflight, including no-Python ablation;
- verified/declared conflicts, unobservable downgrade, and design-absence
  mismatch;
- dataset identity, version, ordering, duplicate IDs, canonical bytes, and
  byte limits.

## Correctness and replay coverage

- independent DAG ordering/hash recomputation;
- independent current-valid Claim edge and citation-integrity recomputation;
- reported versus recomputed verification inconsistency;
- exact reference task/capability/Claim/Evidence/citation/disposition matching;
- semantic hash timestamp exclusion and physical hash sensitivity;
- case-first filesystem authority, tamper rejection, and same-ID zero-call
  replay.
- one-read raw/typed snapshot identity, corrupt hash preservation, case-local
  corruption, mixed/all-failed status, read-before-open bounds, and symlinks;
- fail-closed dangling/stale current edges and reported-assignment independence;
- one-to-one reference matching without reusing an actual item.

## Bounds, comparison, and provenance coverage

- cancellation/deadline and input bounds publish no authority;
- exact MOCK fixture, missing fixture, no REAL mode, and invocation-global
  model-call bound;
- evaluator partial failure and typed metric errors;
- exact metric-definition compatibility, direction-aware thresholds, and zero
  baseline relative delta;
- declared ablation cannot be labelled verified causal.
- strict SUT support kinds and pin-version conflict, planner/runtime
  unobservable pins, and proven design absence;
- Decimal half-even normalization, pre-dispatch metric/model bounds, interrupt
  checks between evaluators, and judge identity validation;
- verified absence/value ablation semantics, canonical spec binding, tamper,
  replay, and concurrent first-writer conflicts for evaluation/comparison/
  ablation authorities.

The full suite remains the Phase 2-6 regression gate. Metrics requiring ranked
retrieval occurrences, invalid candidate plans, real latency/provider telemetry,
or human/model factual labels remain explicitly deferred rather than tested with
fabricated inputs.
