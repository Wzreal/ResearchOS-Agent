# Phase 11 — REAL Evaluation & Quality

## Goal and acceptance criteria

Phase 11 turns Phase 10 structural workflow evidence into bounded, reproducible
REAL evidence for research quality, reliability, latency, cost, and practical
usefulness. It reuses the official `WorkflowFactory` -> `WorkflowCoordinator`
path and Phase 7 evaluation/comparison/ablation authorities.

Acceptance requires:

- A canonical, hash-validated `phase11_real_benchmark@1` with exactly 12 cases.
- Official REAL baseline runs with all terminal results retained, including
  completed, partial, failed, and cancelled cases.
- Measurements expressed as existing Evaluation metrics/case results and derived
  reports, never as a new measurement, lifecycle, retry, budget, checkpoint,
  or workflow authority.
- Two compatible, bounded four-case ablations.
- One human rubric review per output and a blinded second review for a
  predeclared stratified 50 percent audit subset.
- Explicit paid authorization, exact commit/ref pinning, offline-only CI, and
  documentation of results, limitations, and operator protocol.

## Fixed benchmark dataset

Add one checked-in dataset and reference-source manifest. It freezes the query,
as-of date where relevant, source policy, capability requirements, expected
planning/task intents, claims, evidence/citation constraints, verification
disposition, source locators/hashes/license provenance, and authoring version.
It stores metadata and hashes, not credentials, generated outputs, or copied
unlicensed source bodies.

The exactly 12 stable IDs use overlapping tags:

| Case ID | Tags |
| --- | --- |
| `p11_multisource_factual` | factual, multi_source, citation_heavy |
| `p11_comparison_primary` | comparison, multi_source, citation_heavy |
| `p11_time_bounded_public` | public_information, time_bounded, multi_source |
| `p11_evidence_synthesis` | evidence_synthesis, multi_source, factual |
| `p11_conflicting_sources` | conflict_handling, evidence_synthesis, citation_heavy |
| `p11_citation_chain` | citation_heavy, factual, primary_source |
| `p11_multistep_investigation` | multi_step, multi_source, evidence_synthesis |
| `p11_comparison_time_bounded` | comparison, time_bounded, public_information |
| `p11_conflict_multistep` | conflict_handling, multi_step, multi_source |
| `p11_public_evidence_synthesis` | public_information, evidence_synthesis, citation_heavy |
| `p11_primary_source_investigation` | primary_source, multi_step, factual |
| `p11_complex_citation_comparison` | comparison, citation_heavy, multi_source |

Use existing `EvaluationDataset`/`EvaluationCase` canonicalization. Full
reference cases include all existing reference blocks; execution conditions are
used only for compatibility, never Run authorization.

## REAL composition and operator protocol

Add a Phase 11 configuration-only bundle that references existing
`Phase10WorkflowProfileV1` semantics, explicit REAL composition, capabilities,
benchmark/evaluation identities, declared commit/version, and frozen run limits.
Add `WorkflowFactory.build_real(...)` to compose existing RunManager, REAL
integration, bounded DeepSeek planning/agent/claim-extraction/verification,
configured REAL capabilities, Phase 10 coordinator, and Phase 7 harness.

Keep `WorkflowCoordinator` unchanged unless an actual blocker is demonstrated.
Do not alter `WorkflowRuntimeHandoff` v1, `RuntimeCheckpoint` v1, `RunConfig`
v1, ClaimGraph snapshots, or another Phase 1-10 authority merely to label a
benchmark Run. `run --mode real`, `resume`, and the new explicit benchmark
operator command must use the same official factory path and never fall back to
MOCK or manually stitch services.

Every paid batch fails closed before dispatch unless it has:

- clean working tree;
- `--expected-commit-sha <40-hex>` equal to current HEAD;
- an approved Phase 11 branch/ref or `main`;
- explicit REAL acknowledgement;
- `--approved-cost-microunits <N>`; and
- protected configuration and credentials.

USD 50 is only an absolute Phase 11 ceiling, not authorization. The approved
amount must be positive and at or below that ceiling. Before each next REAL run,
its frozen upper-bound reservation must fit remaining approved capacity.
UNKNOWN dispatched usage remains consumed and non-reallocatable. Formal
evidence may be collected pre-merge only from an approved, exact, clean commit;
ordinary CI stays offline with zero paid calls.

## Evaluation and metrics

Reuse `EvaluationHarness`, `EvaluationRun`, filesystem evaluation publication,
`EvaluationComparisonService`, and `AblationService`. Derive inputs read-only
from frozen Run state, trace, checkpoint, Evidence, ClaimExtractionOperation,
ClaimGraph, VerificationOperation, VerificationResult, and existing
provider/runtime observations. A normalized internal view, if needed, is
transient evaluator input and never an independent persisted authority.

Extend the Phase 7 reader/evaluator projection only where Phase 10 artifacts
are needed. Report these separate metric families:

- Structural/deterministic: DAG validity, task outcomes, retries, budget,
  claim/evidence support, unsupported claims, citation integrity, source
  diversity, and verification consistency.
- Reference: expected claims/tasks/capabilities, evidence constraints,
  citation correctness/coverage, and expected disposition.
- Reliability: terminal distribution, provider/tool failure codes, recovery,
  dispatched-unknown count, and no-retry-unknown conformance.
- Latency: total wall time and planning, execution, claim-extraction,
  verification, and evaluation durations; unavailable timing stays unavailable.
- Cost: observed tokens/calls/tool calls/microunits where durable artifacts
  support them, upper bounds separately, and UNKNOWN separately. Never invent
  a zero, observed cost, or releasable capacity.
- Human/reference: factual support, citation correctness/coverage, source
  quality/diversity, synthesis usefulness, and conflict handling.

Evaluation identities bind dataset, compatible Run bindings, policy, evaluator
bundle, metric definitions, declared system commit/version, SUT observations,
and frozen input manifests. Derived reports cite these authorities only.

## Human review

Use rubric `phase11-human-rubric@1` with a 0-2 score per quality dimension,
evidence references, and bounded rationale. One reviewer assesses every
baseline and ablation output. A second blinded reviewer assesses a predeclared
stratified 50 percent subset spanning required tags and both ablation arms.

Record reviewer identity or approved pseudonymous ID, rubric version,
timestamps, output/evaluation identity, disagreement state, adjudication
provenance, and final result with the corresponding Phase 7 evaluation input or
result. Critical factual/citation disagreements and all two-point differences
are adjudicated; noncritical one-point differences are retained and summarized.
Human judgments remain separate from deterministic and model metrics.

## Ablations

Run exactly two four-case paired ablations through existing explicit
configuration boundaries:

1. `verification.max_rounds=1` versus `max_rounds=2`, asking whether a second
   verification round improves factual support/citation quality enough to merit
   its latency and cost. No verification-disabled coordinator branch is added.
2. `web_search + web_browser` versus `web_search` only, asking whether browser
   retrieval improves reference and human citation/source-quality outcomes.

Before REAL execution, select four baseline case IDs per ablation and construct
one deterministic four-case subset `EvaluationDataset` identity/hash. Evaluate
existing compatible baseline Run artifacts against that same subset and evaluate
the candidate arm against the same subset, ordered case IDs, policy, metric
definitions, and evaluator bundle. Reuse paid baselines when exact
configuration/provenance permits; do not rerun controls merely to recreate them.
Use existing comparison/ablation validation and label any insufficiently
verified intervention as declared association.

## Tests, failure handling, and delivery

Offline tests cover benchmark/reference canonicalization, REAL bundle
validation, mocked real-factory composition, operator admission and commit/ref
pinning, reservation caps, read-only derived metrics, UNKNOWN usage, reviewer
validation, compatible subset construction, comparison/ablation validation, and
resume/no-duplicate behavior. Protected operator-only REAL tests require
acknowledgement and paid approval and are excluded from CI.

Unknown provider dispatches never retry outside existing durable semantics.
Corrupt/missing frozen artifacts explicitly fail the affected evaluation;
dataset/profile/composition/commit drift rejects continuation; partial/failed
Runs remain in reliability and cost coverage reports.

Expected work is limited to a Phase 11 benchmark/reference fixture and rubric,
configuration bundle, narrow real-factory composition, CLI/operator command,
Phase 7 read-only projection/evaluators, tests, protected operator workflow, and
README/RESULTS/TASKS/DECISIONS/runbook updates. Target production-code effort is
30-50 percent of Phase 10; emphasis is REAL execution, evaluation, review,
analysis, and documentation rather than new architecture.

Implementation order:

1. Add canonical benchmark/reference/rubric and REAL config bundle with tests.
2. Add `WorkflowFactory.build_real(...)` and explicit CLI admission path.
3. Add read-only Phase 7 projections/evaluators and subset comparisons.
4. Add reviewer provenance validation and derived reporting.
5. Add protected exact-commit REAL protocol, run approved batches, and publish
   results, limitations, and operator documentation.

Non-goals: UI/API product work, distributed execution, generic agent framework,
another workflow state machine, automatic online benchmark service, massive
benchmark/ablation matrix, Phase 12 planning, a release/tag, or fabricated
quality/cost claims.

## Changes from previous PLAN11

- Fixed the benchmark at exactly 12 multi-label cases.
- Removed the standalone measurement authority; reuse Phase 7 metrics/results.
- Changed verification ablation from on/off to one versus two rounds.
- Defined compatible deterministic four-case subset datasets and baseline reuse.
- Made paid approval explicit, keeping USD 50 only as a hard ceiling.
- Replaced main-only execution with protected exact-commit execution on an
  approved Phase 11 ref or main.
- Bounded human review to universal first review plus stratified 50 percent
  blinded audit and critical-disagreement adjudication.
- Kept coordinator and frozen authorities unchanged; limited REAL work to
  configuration/composition and required factory support.
