# Phase 9 Results, Costs, and Limitations

## Phase 11 REAL benchmark result template

No Phase 11 paid benchmark has been run. Portfolio-v1 may publish one bounded,
explicitly authorized REAL smoke result without claiming that all 12 cases were
run. When separately authorized, publish
the dataset/bundle/profile/configuration pins, exact commit, source policy,
Run IDs, metric provenance, and observed cost/latency. Keep UNKNOWN or
unavailable values explicit. `structural_selfcheck_v1` is not Phase 11 quality
evidence.

## Evidence vocabulary

ResearchOS publishes four deliberately separate kinds of evidence:

- **OBSERVED** — a validated value in an actual Run or provider artifact.
- **DETERMINISTIC_OFFLINE** — a reproducible fixture, evaluator, or offline
  gate result. It is not a REAL performance or quality benchmark.
- **UPPER_BOUND** — a frozen reservation or pricing-safety limit used for
  admission. It is not an invoice.
- **NOT_MEASURED** — no authoritative data exists. It is never displayed as
  zero, a success rate, a latency, a cost, or a quality conclusion.

The repository currently has no representative licensed benchmark dataset,
repeated REAL-run study, human rubric study, provider invoice reconciliation,
or REAL end-to-end measurement. All REAL quality, latency, throughput, and
cost claims are therefore **NOT_MEASURED**.

## Published deterministic baseline

Run this read-only command against the checked-in corpus to produce a local,
ignored occurrence record. It makes no network call, reads no credential, and
does not mutate the corpus.

```shell
uv run python scripts/measure_release_baseline.py \
  --corpus tests/fixtures/schema_v1 \
  --output outputs/measurements/offline-release-baseline.json
```

The JSON occurrence contains timestamp, duration, OS, and Python details for
local troubleshooting only. Those fields are intentionally omitted below: they
are not machine-independent baselines and must not be compared as performance
data.

<!-- BEGIN schema-v1 semantic baseline -->
### Frozen schema-v1 semantic baseline

Classification: **DETERMINISTIC_OFFLINE**. These are fixture and authority
facts, not REAL quality, latency, throughput, or cost benchmarks.

- Artifact files: `11`
- Artifact bytes: `91008`
- Corpus hash convention: SHA-256 of checked-in canonical persisted bytes;
  the corpus is Git binary/no-EOL-conversion data.

| Authority | Stable identity / hash |
| --- | --- |
| Run | `run_1` rev `3`; input `8d078d5956a98f8d1d9ff455e614690d1305aac5ba302fd00a9a3da398f41f51`; config `d9e24948dd68c8ee28fbc62770a55ecfcc2b050c8c08fee6be488d056848ffe8` |
| Checkpoint | `dag_runtime`; DAG `bafe4202d1e1b769116085081836afc3a5e9f9acf9752a85a3b3829318255289`; payload `3dc7ac743a68594ed4de229a4398c687f095a34995be04542e1a693b58788b60` |
| Evidence | rev `1`; snapshot `360ebb4fc7308ebc570da9fb17ea8d6a0298cca1db4aa0c008830a2a043982e1` |
| Claims | rev `2`; snapshot `122531f0a5798e22c8a3f1ac73d3214b517ec76d2e5555ef87035bfc4c1bec66` |
| Verification | `ver_df371592a929c30447d7fa82b5eab3d7310a0c0ce7367f838b0d4a27b75bfdb4`; authority `5da6f1890302deee86542b422782a01d47ca83b6f5d8055bf8087395c45753e2`; report `e54aa0939f3b555f7fb7abf7133433e111cbd99b499dad67e00e80e979a5c4f8` |
| REAL composition | `realcomp_42aa8568aa8a770a96fc88ac1983ad67e5fa8a3f09dd92845119bb2acb029b1d`; semantic `69ba06abfe23c09051d37762a51e94cfcd97ab943563edc933272052412e6f71`; authority `e27d3b3a95ad65b6460b93db3247fb7e15f5a2839b7acc301b642ec78d116aec` |
| Evaluation dataset | `schema_v1_eval` v`1`; `bd0ac741a74f5548cb109d22ed5da31fcc5f01f6ec6aba2cc7f60fa612d2cc9b` |
| Evaluation | `eval_6f64dc66d60d6c30356689529c0f07936d02c3ec811370603a58c187492756ce`; semantic `e3a2faf4b1d91174aa65ec84699c605a66f6ff30c78a8bb2cf5736352f0554b7`; authority `ff9b86bc82cd91cd123f121f3619b1f38f01ac75c659048e8368b1f10f228f8b` |

#### Typed evaluation results

Case `schema_v1_case` for Run `run_1` (semantic `bcbf34e35e4439a3f57dcc192c551eb107fdd73ba106cf1aae1e1814eac639ae`, authority `8fc467cb60d4920f1572a81e61a8ce69a3ad14777f9b579003ec61089db9a4a5`):

| Metric | Definition hash | Status | Type | Value |
| --- | --- | --- | --- | --- |
| `citation_integrity_valid` | `eb0a0782886510ceb053c3ac41f3de1d817b5de31db5fd1134d240ec84a49a9d` | `computed` | `boolean` | `True` |
| `evidence_claim_support_ratio` | `d70c09ba842b5578fc8f55f46bc157d462a227effae55736c7aa0103f81aa658` | `computed` | `ratio` | `1.0` (`1/1`) |
| `evidence_conflict_claim_ratio` | `a685cd62ae868023eab9128b36942f7380da64af61687be67fc2074fb9cc979f` | `computed` | `ratio` | `0.0` (`0/1`) |
| `evidence_exact_duplicate_ratio` | `63a14b77c7d7b2e6280630dbb4dc9e0a3f123fba7d53677c384d4488f6551509` | `computed` | `ratio` | `0.0` (`0/1`) |
| `evidence_unsupported_claim_ratio` | `e09ab933653a77a7a1611dec28fbf985e2e4ef80a2dad3162ef2a32c05f556df` | `computed` | `ratio` | `0.0` (`0/1`) |
| `execution_blocked_task_ratio` | `cb69c5540b3432cca16ef6736d019121c56945214c2b74931f99c9f7839c9a44` | `computed` | `ratio` | `0.0` (`0/3`) |
| `execution_budget_adherent` | `7e1ce0be4b48bcb6c3ee2c36d3e8e3062a0cef630ca9fc6440257f93c9c2e76a` | `computed` | `boolean` | `True` |
| `execution_retry_count` | `3839efd55cdaaf7d3bb85aeb9b45b1fd940f6c7d28b010501b8c7d095bb513bb` | `computed` | `integer` | `0` |
| `execution_task_success_ratio` | `070c3653c4ef33799d2bdda73b99d68f64241b5a2e90246814fb74f982778913` | `computed` | `ratio` | `0.0` (`0/3`) |
| `planning_capability_feasible` | `b76e990df932a6259e9cf0720b42b60c12221f95b4c072bb79e5a8a55e4910d5` | `computed` | `boolean` | `True` |
| `planning_dag_valid` | `ac70f81d4233606cf62b70f655fcac7a94865880689118872c184cd7bb526989` | `computed` | `boolean` | `True` |
| `planning_task_count` | `2a1db9f29fd1c5178c1552fa3c4d50575130bff28f93dd0a08db13d5c61da0ea` | `computed` | `integer` | `3` |
| `verification_citation_completeness` | `ac33a88d6d8397ef2b36ce1ffee72971d0bf4e04803795caa55b7163baa153af` | `computed` | `ratio` | `1.0` (`1/1`) |
| `verification_disposition` | `063c70ab361b4a6a5184fb41ecd0c799d91ce3049bc430346d797ae2a5d8f0cc` | `computed` | `enum` | `verified` |
| `verification_publishable_claim_ratio` | `cc6d613e16892fa91a1b3ec5b62d3c902fb6e07301d42bd8cf7cfee8bc2a671e` | `computed` | `ratio` | `1.0` (`1/1`) |
| `verification_reported_consistency` | `9608229867d3bce121c729a3e30ad60210a58fadf644da74f1478aa19a6a4e14` | `computed` | `boolean` | `True` |
| `verification_unresolved_finding_ratio` | `96f3a6a083d8814e5e9694573d68a969019d24ba4eac05d1abd70e8ae00e2601` | `not_applicable` | `non_computed` | `zero_denominator` |

#### Artifact byte hashes

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `evaluation_datasets/schema_v1_eval/1.json` | `839` | `38e380117387db3e8a4e1f5e6be96581295e18d0dae06beb35a6d656c43798b7` |
| `evaluations/eval_6f64dc66d60d6c30356689529c0f07936d02c3ec811370603a58c187492756ce/cases/schema_v1_case.8fc467cb60d4920f1572a81e.json` | `20593` | `96eacb03f333967d80b2fcc7537f66239402252a3e3bac0dd8c435d23703f045` |
| `evaluations/eval_6f64dc66d60d6c30356689529c0f07936d02c3ec811370603a58c187492756ce/evaluation.json` | `38601` | `bf04ef2900ce8c9e0d3b2f2f425cba475c099928d339196fb0bcfc548f7ab076` |
| `run_1/checkpoint.json` | `4840` | `a4d18d44c42e778c0f1c6cd19340ea1574262721343d84939d77a29b2571627f` |
| `run_1/claims.jsonl` | `2666` | `cc12fc6607d924db3b6d21d5603257dc8d654c03c028cb42f311a4e6f15cdd09` |
| `run_1/evidence.jsonl` | `2495` | `7649a2c2048448cde2b2459629be525425777d1ec77addd7d93cbb9f4d42d4b6` |
| `run_1/real_composition.json` | `8476` | `0d51e627494726bc20fab60f638dbd7764a3326ed3d4c037b5cb0ce646d9cc6e` |
| `run_1/report.md` | `407` | `e54aa0939f3b555f7fb7abf7133433e111cbd99b499dad67e00e80e979a5c4f8` |
| `run_1/run_state.json` | `1024` | `564dcf128dc42e7959d57a3c0fdc75c42ac3882b6b522968301296232730c59f` |
| `run_1/trace.jsonl` | `3593` | `56e366283691e721c436aeeeff05ce5edac3a3e81c57ace1269752860d0e0fac` |
| `run_1/verification.json` | `7474` | `5c101afad1963da3afa847ac0eac110bf82007a76024a142a061930dd5748ae6` |

<!-- END schema-v1 semantic baseline -->

## Cost interpretation

| Integration | What ResearchOS can report | What it cannot report |
| --- | --- | --- |
| DeepSeek | Validated provider token counts are **OBSERVED** when persisted. Local currency calculation and pre-dispatch reservation are **UPPER_BOUND**, pinned by the REAL composition. | Invoice, discounts, cache billing, tax, or exact monetary charge. |
| Tavily | Frozen per-credit reservation is an **UPPER_BOUND**. | Actual credit usage or invoice without provider evidence. |
| Browser | Tool monetary reservation may be zero. | Network egress, proxy, or third-party service cost. |
| Zilliz | Attested Free-plan profile has a zero-cost **UPPER_BOUND** only. | Plan inference from hostname, paid Serverless/Dedicated cost, or invoice. |
| OTLP | No Run-budget charge is modeled. | Collector, SaaS, retention, or egress cost. |

Reservation is admission data, not a bill. A real smoke result is a single
connectivity/structured-planning observation and is never a system benchmark.

## Published limitations

- The Python Tool accepts trusted supported code; it is not a hostile-code
  sandbox.
- The Browser is a bounded HTTP capability with SSRF controls, not a general
  browser isolation boundary.
- REAL LLM use is restricted to the canonical DeepSeek public origin. The
  protected REAL smoke verifies one planning call, not a full REAL E2E flow.
- Claim creation is application-controlled; automatic Claim extraction is not
  implemented.
- Managed retrieval is read-only, attested-Free-plan BM25 only; there is no
  ingestion, embedding, dense/hybrid retrieval, or reranker.
- OTLP export is non-durable best effort; canonical local trace remains the
  authority.
- Unknown schemas and corrupted artifacts fail closed; no general migration
  framework exists beyond the checked-in schema-v1 compatibility corpus.
- A dispatched provider operation can retain `UNKNOWN` usage after interruption;
  it is never silently converted to zero or transparently retried.
