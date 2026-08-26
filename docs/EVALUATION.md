# ResearchOS Agent Evaluation Plan

**Status:** Metric design only. No experiments have been run and this document
contains no measured results.

## Principles

- Evaluate immutable run artifacts with versioned datasets and evaluator code.
- Separate retrieval, trajectory, and report quality to avoid hiding failure in
  an aggregate score.
- Record metric inputs, configuration, evaluator version, uncertainty, and
  skipped reasons.
- Use deterministic offline fixtures for unit/contract tests and clearly label
  model-assisted or human evaluation.
- Never substitute missing data with zero or invent a benchmark value.
- Compare ablations on identical inputs, budgets, seeds where applicable, and
  evaluation policy.

## Evaluation unit and artifact contract

The primary unit is a completed or explicitly partial `run_id`. Evaluation reads
the request/configuration snapshot, task DAG and attempts, trace, evidence,
claims/edges, report, and artifact manifest. Dataset examples require stable
example IDs, licenses/provenance, expected evidence or rubric where available,
and a version.

Results should distinguish:

- `computed`: metric has valid inputs and a value;
- `skipped`: policy intentionally excludes the metric, with reason;
- `unavailable`: required artifact or annotation is absent;
- `error`: evaluator failed and includes a typed error.

## 1. Retrieval evaluation

Candidate metrics, used only where suitable ground truth exists:

- **Recall@k:** relevant evidence retrieved within the first `k` results.
- **Precision@k:** fraction of the first `k` results judged relevant.
- **nDCG@k / MRR:** rank-sensitive relevance performance.
- **Source diversity:** distribution across independent domains/source types,
  reported with the source-classification rule.
- **Evidence freshness:** age relative to query time when recency is required.
- **Deduplication rate:** exact/near duplicates removed, with threshold/version.
- **Provenance completeness:** required provenance fields present and valid.
- **Claim coverage:** fraction of evaluated atomic claims with at least one
  admissible supporting evidence edge.
- **Conflict discovery recall:** known contradictory evidence surfaced, only on
  datasets annotated for conflict.

Relevance and admissibility judgments must state whether they are human labels,
rules, or model-assisted assessments.

## 2. Agent trajectory evaluation

Candidate metrics:

- **Task completion rate:** committed successful tasks divided by eligible tasks,
  reported alongside blocked, failed, and cancelled counts.
- **DAG validity:** structural and policy validation outcomes before execution.
- **Critical-path efficiency:** observed duration versus declared critical path,
  interpreted with concurrency and provider latency.
- **Retry efficiency:** recovered transient attempts versus retries consumed.
- **Failure isolation:** independent tasks completed despite branch failure.
- **Resume correctness:** committed outcomes not repeated after fault injection.
- **Budget adherence:** runs/tasks staying within time, token, cost, and tool-call
  limits, including reservation overshoot.
- **Tool success and error taxonomy:** per capability and adapter.
- **Replan utility:** failed/blocked conditions resolved per bounded replan,
  paired with added cost.
- **Trace completeness:** required lifecycle transitions represented and
  correlated.
- **Redundant work rate:** semantically duplicated tasks/tool calls under a
  documented detector.

Latency and cost are operational metrics, not quality proxies. Partial runs must
remain in denominators where the metric definition requires them.

## 3. Report quality evaluation

Candidate metrics and rubric dimensions:

- **Citation correctness:** cited evidence entails or appropriately qualifies
  the associated claim.
- **Citation completeness:** externally verifiable claims that require evidence
  have citations.
- **Claim factuality:** agreement with admissible reference evidence.
- **Conflict handling:** material contradictions are resolved or disclosed.
- **Uncertainty calibration:** confidence and caveats match evidence strength.
- **Question coverage:** required perspectives and subquestions are addressed.
- **Synthesis quality:** report integrates sources rather than concatenating
  summaries.
- **Coherence and structure:** organization supports the requested use case.
- **Instruction adherence:** format, scope, source, and budget constraints.
- **Unsupported-claim rate:** atomic claims without admissible support.

Human review uses a versioned rubric with blinded samples and, when feasible,
multiple raters. Inter-rater agreement is reported rather than assumed.
Model-based judges require pinned prompts/models where possible, calibration
against human labels, and sensitivity checks; they are not ground truth.

## 4. Ablation plan

Planned comparisons include:

- checkpoint/resume enabled versus clean uninterrupted execution;
- single-perspective versus multi-perspective planning;
- evidence deduplication on/off;
- claim graph verification versus transcript-only synthesis baseline;
- conflict detection on/off;
- bounded Red/Blue/Judge verification on/off or differing fixed round caps;
- dynamic replanning on/off;
- retrieval strategies and top-k values;
- selected adapters under equivalent capability and budget constraints.

Each ablation declares one intended independent variable, fixed controls,
dataset version, run count, randomization/seeds where applicable, budgets, and
statistical method before execution. Results must include failures and resource
trade-offs.

## 5. Observability requirements for evaluation

The evaluation harness requires structured events for run/task transitions,
planner decisions, attempts, tool calls, evidence/claim changes, checkpoints,
budgets, verification decisions, and artifact publication. Correlation IDs must
connect a report claim to evidence, producing task, tool call, and run.

Sensitive data is redacted before evaluation artifacts are stored. Evaluator
failures are traced separately from failures in the evaluated run.

## 6. Future evaluation gates

Numeric release thresholds are intentionally deferred until representative,
licensed datasets and baseline runs exist. Before Phase 9, the project must:

1. approve dataset and rubric versions;
2. run and preserve reproducible baselines;
3. select metrics resistant to gaming for each release gate;
4. document confidence intervals or sample limitations;
5. publish measured thresholds through an ADR or release policy.

No Phase 0 claim should be interpreted as an experimental result.
