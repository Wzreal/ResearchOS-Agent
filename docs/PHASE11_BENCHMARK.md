# Phase 11 benchmark and review protocol

`phase11_real_benchmark@1` has exactly 12 fixed multi-label cases. Its
identity is the canonical `EvaluationDataset` version and content hash; source
policy and reference annotations are explicit case metadata. The benchmark is
not generated from provider output.

Portfolio-v1 REAL validation is closed after three bounded Planning smoke
attempts. The final attempt dispatched DeepSeek Planning, received a response,
and validated a three-task DAG; it stopped before Agent or tool dispatch when
fixture-keyed execution-policy configuration did not cover the planner's
dynamic task IDs. The deterministic integration defect is fixed and covered by
offline regression tests, but no further paid retry is authorized under the
bounded portfolio budget.

The other eleven frozen cases and both ablation datasets remain available but
unexecuted in REAL mode. This is not a claim that any complete REAL benchmark
case, Tavily search, evidence pipeline, claim extraction, verification, or the
12-case benchmark succeeded.

The two fixed four-case ablations are `max_rounds_1_vs_2` and
`search_browser_vs_search_only`. Reuse a baseline REAL run only when its
durable configuration, source policy, profile hash, and provenance match the
comparison exactly.

Human review uses `Phase11ReviewRubricV1` and `Phase11HumanReviewV1`: one
review for every output, a blinded secondary review for the predeclared
stratified 50% subset, and adjudication for critical disagreement. Record
reviewer, rubric/version, timestamp, output hash, and blinding state.
