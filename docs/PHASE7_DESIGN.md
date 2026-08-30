# Phase 7 Design - Read-only Agent Evaluation Harness

Phase 7 evaluates frozen Phase 2-6 artifacts. It never mutates an evaluated
Run and owns no scheduler, retry, checkpoint, or durable budget ledger.

## Semantic locks

1. Dataset reference annotations affect metrics only. Only an explicit
   `RequiredExecutionConditions` block participates in case/run compatibility.
   Source policy requirements are `UNSPECIFIED`, `REQUIRE_NONE`, or
   `REQUIRE_EXACT`; `None` never carries two meanings.
2. Every case/run binding is checked before any evaluator or model call. The
   case query is prepared with the existing Phase 1 persistence boundary and
   its canonical `RunInput` hash must equal `RunState.input_hash`.
3. One EvaluationRun has one homogeneous SUT pin set. Value conflicts fail;
   unavailable observations only downgrade provenance; absence is not a value.
4. Invocation limits cover cases, metrics, input/output bytes, duration, and
   the global number of model calls. Cancellation or deadline expiry publishes
   no authoritative EvaluationRun.
5. Metric values are a strict discriminated union. Missing, inapplicable,
   skipped, and evaluator error are never encoded as zero.
6. The authority snapshots every metric definition. Evaluator bundle identity
   binds evaluator/metric/normalization/aggregation versions, and comparisons
   require exact definition hashes.
7. `evaluation_semantic_hash` excludes timestamps and persistence occurrence;
   `artifact_content_hash` covers the complete persisted artifact except
   itself. Comparison identity uses semantic hashes.
8. Deterministic correctness is recomputed from the lowest authoritative
   artifacts. Reported DAG, citation, edge, publication, and disposition state
   is diagnostic input, not evaluator authority.
9. Same-input authority replay makes zero evaluator/model calls. Filesystem
   publication writes case artifacts first and `evaluation.json` last.
10. Model evaluation is an optional provider-independent port. Phase 7 ships
    exact MOCK fixtures only, with no retry, real adapter, or fallback.
11. Comparison and ablation require the same dataset, cases, evaluation policy,
    and metric definitions, and propagate the weakest provenance grade. A
    declared-only ablation is an association, never a verified causal result.
12. Each artifact is read once under the effective reader/request byte bound;
    parsing consumes those exact bytes. Malformed artifacts are `CORRUPT`, not
    absent, and retain raw hash/size. Case-local corruption produces `FAILED`
    while healthy Cases continue.
13. Metric numeric normalization is Decimal half-even v1 at twelve places for
    values, aggregation, and comparison. Ratio counts are exact integers.
14. Evaluation, comparison, and ablation stores are immutable first-writer
    authorities. Same bytes replay; different same-ID bytes conflict.

## Current artifact limitations

The repository does not persist invalid PlanningResult candidates, retrieval
rank/query occurrence, a complete run manifest, or a fully verifiable system
configuration bundle. Metrics that require those inputs remain unavailable or
deferred rather than being inferred from prose or trace summaries.

## Architecture and ownership

`EvaluationHarness.evaluate_existing` is the sole application coordinator. It
loads a canonical dataset, freezes the selected Run directories through
`ReadOnlyRunArtifactReader`, validates case/run compatibility and homogeneous
SUT pins, then invokes enabled deterministic, reference, and optional model
evaluators. Aggregation, comparison, and ablation are independent pure services.
Filesystem and memory stores implement the same narrow authority interfaces.
No Phase 3 scheduler, retry, checkpoint, Run transition, or budget settlement
is called by this flow.

## Contracts and identity

- `EvaluationCase` separates `ReferenceAnnotations` from
  `RequiredExecutionConditions`; only the latter enters preflight.
- `EvaluationPolicy` bounds cases, metrics, dataset/input/output bytes, model
  context/response bytes, invocation duration, nonterminal access, enabled
  evaluator IDs, and the global model-call count.
- `MetricValue` is a discriminated union of boolean, integer, float, ratio,
  enum, and non-computed states. Computed metrics carry typed evidence refs and
  certainty; non-computed metrics carry an explicit reason.
- `MetricDefinitionSnapshot` pins value kind, direction, evaluator, versions,
  normalization, applicability, rounding, and aggregation semantics.
- `eval_run_id` binds dataset, homogeneous SUT, policy, evaluator bundle, case,
  Run, and frozen manifest identities. Model-backed bundle identity also binds
  the persisted `evaluation_model_bundle_hash`.
- Semantic hashes omit timestamps. Artifact hashes include timestamps and all
  persisted content except the hash field itself.

## Deterministic correctness and reference evaluation

The deterministic evaluator rebuilds DAG ordering validity, current-valid Claim
edges, citation integrity, publication eligibility, and disposition from the
lowest available snapshots. Persisted reported conclusions are compared only as
diagnostics. Exact reference matching normalizes task objectives and Claims,
matches Evidence using source/content constraints, and requires a current-valid
typed edge for citation recall. Reference fields never authorize or prohibit a
Run.

## Persistence and replay

The filesystem layout is
`outputs/evaluations/<eval_run_id>/cases/<case_id>.<hash-prefix>.json`
followed by
`evaluation.json`. Comparison and ablation authorities live under their own
identity directories. Every file uses canonical JSON plus one newline and an
atomic create-if-absent claim; authority is written last. Loading validates typed contracts, identity,
semantic/content hashes, defensive persistence safety, and equality of case
files with authority. A complete same-ID authority returns before evaluator or
model invocation. Missing authority means incomplete case files are ignored and
the evaluation is safely recomputed.

## Comparison and ablation

Regression policies name an aggregate statistic, direction-aware threshold,
and missing-metric behavior for each exact definition hash. Relative change
against a zero baseline is `NOT_COMPARABLE`, never infinity or fabricated zero.
Ablation validation requires common dataset/policy controls, exact persisted
config hashes, and checks declared changed pins against observed SUT pins. An
artifact-verified value change or an explicitly declared artifact-confirmed
absence can be a verified intervention. Unchanged controls must be
artifact-verified or identically proven absent; declarations and unobservable
pins downgrade the result.

## Failure model and deferred work

Corrupt datasets, incompatible bindings, SUT value conflicts, unsafe or changed
inputs, metric contract violations, persistence conflicts, and tampered
authorities are typed failures. A corrupt Run artifact fails its Case without
aborting healthy Cases. Individual deterministic or ordinary model evaluator
failure, including a missing fixture, becomes explicit per-metric `ERROR` and a
partial case. Invocation
cancellation, deadline, global model bound, or input mutation aborts without
publishing authority.

Phase 7 intentionally defers CLI infrastructure, real providers, benchmark
datasets/results, trace export, durable evaluation budgets, ranking metrics
without retrieval occurrences, statistical significance, confidence intervals,
and composite release scores.
