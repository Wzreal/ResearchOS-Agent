# Phase 6 Semantic and Test Coverage Audit

Audit date: 2026-08-29. Scope is Phase 6 only. `COVERED` means the approved
semantic lock was already correct and now has explicit evidence. `FIXED` means
the audit found and corrected an implementation or contract gap. `DEFERRED`
is limited to an explicitly later-phase integration. No P0/P1 gap remains.

| Requirement | Implementation | Test | Status | Gap |
|---|---|---|---|---|
| Exact verification identity | Hashes run/revision, both snapshot hashes, policy hash, model-bundle hash | `test_identity_formula_and_all_change_dimensions` | FIXED | None |
| Stable synthesis/report/citation identities | Immutable discriminators and revision/source/content pins | `test_entity_identity_formulas_exclude_model_prose_and_round` | FIXED | None |
| Stable finding/acquisition identities | Excludes round, prose, rationale, and attempts | `test_entity_identity_formulas_exclude_model_prose_and_round` | COVERED | None |
| Freeze/load boundary | Each live store loads once; roles receive frozen models only | `test_live_stores_load_once_and_roles_use_frozen_snapshots` | FIXED | None |
| Frozen input isolation | Later live mutation cannot change role context | `test_frozen_role_context_is_unchanged_by_later_live_mutation` | COVERED | None |
| Publication fingerprint guard | Narrow revision/hash boundary rejects changed input | `test_snapshot_change_after_freeze_prevents_publication` | FIXED | None |
| Snapshot citation/conflict processing | Snapshot-backed validator and pure frozen conflict derivation | load-count and Phase 5 citation tests | COVERED | None |
| Whole Claim/Evidence admission | Complete record+revision accepted or omitted | `test_whole_item_exact_and_one_byte_boundaries` | COVERED | None |
| Omission set invariants | Deterministic, disjoint selected/omitted sets; omitted linked evidence retained | whole-item tests | FIXED | None |
| Omission affects disposition | Omission prevents VERIFIED | `test_all_final_dispositions` | COVERED | None |
| Model context byte bound | Canonical UTF-8 request checked before dispatch | `test_model_context_bound_rejects_before_dispatch` | COVERED | None |
| Raw response byte bound | Raw bytes checked before JSON/Pydantic parsing | `test_all_byte_bounds_reject_before_publication` | COVERED | None |
| Report/artifact byte bounds | Both enforced before authoritative write | byte-bound and individual-string tests | COVERED | None |
| Individual string bounds | Section, Claim, finding, acquisition prose constrained | `test_individual_string_bounds_and_many_strings_artifact_bound` | COVERED | None |
| Runtime structural support | SUPPORTS-only/support+contradiction/no-support/context-only table | `test_runtime_structural_support_truth_table` | COVERED | None |
| Model cannot assert support | Synthesis schema forbids extra support state | `test_model_cannot_supply_structural_support_field` | COVERED | None |
| Exactly one ReportClaim | Missing, duplicate, invented, and extra Claims fail closed | `test_synthesis_requires_exactly_one_report_claim` | COVERED | None |
| REMOVE semantics | Entity/ID retained; state REMOVED; membership/citations cleared | `test_remove_preserves_identity_and_clears_citations` | FIXED | None |
| Draft lineage | Continuous revisions, exact parents, deterministic IDs/hashes, final pin exists | `test_keep_round_creates_continuous_deterministic_draft_lineage` | FIXED | None |
| Stable section identity | Title/prose changes do not change section/report identity | identity tests | COVERED | None |
| Red finding contracts | All four types and required/forbidden pins | `test_all_red_finding_types_accept_valid_pins` and contract tests | COVERED | None |
| Red frozen pin authorization | Invented and wrong-Claim pins rejected | invented-pin and cross-Claim citation tests | FIXED | None |
| Blue action coverage | KEEP/QUALIFY/REMOVE/ADD/REQUEST independently exercised | `test_every_blue_action_has_bounded_effect` | COVERED | None |
| Blue authority boundary | No full draft, invented citation, conflict, Tool/store mutation | Blue contract/action tests | FIXED | None |
| Judge boundary | Exact current draft, exact Claim set, no prose/evidence/action fields | Judge stale/content tests | FIXED | None |
| SUPPORTED constraint | Only STRUCTURALLY_SUPPORTED can be SUPPORTED | Phase 6 contract tests | COVERED | None |
| Bounded loop | Synthesizer plus at most three calls per round | continue/finalize and max-round tests | FIXED | None |
| Finding quota/deduplication | Unique stable findings consume quota; exhaustion is non-VERIFIED | max-finding and duplicate-round tests | FIXED | None |
| Acquisition cannot drive CONTINUE | Continue reason must match open finding or frozen conflict | pending-acquisition test | FIXED | None |
| Four final dispositions | VERIFIED/PARTIAL/REJECTED/INCONCLUSIVE predicates tested independently | `test_all_final_dispositions` | COVERED | None |
| Publishable predicate | Runtime-derived state/verdict/finding/citation predicate | publishable/open-finding/global-error tests | FIXED | None |
| Operational failures | Failure, timeout, cancellation never become INCONCLUSIVE | operational boundary tests | COVERED | None |
| Role success and failure | All roles succeed in E2E and fail once without internal retry | provider/malformed parametrized tests | COVERED | None |
| Role cancellation/deadline | Before/after dispatch and never-return timeout for every role | role-parametrized async tests | COVERED | None |
| Usage certainty | EXACT/UPPER_BOUND aggregate; UNKNOWN stops or terminates at Judge | usage certainty tests | FIXED | None |
| Budget ownership | RunState budget remains immutable; no second ledger exists | known-usage test and architecture inspection | COVERED | None |
| Exact mock fixtures | verification/role/round/draft key, missing key and response mismatch fail | mock fixture and identity tests | FIXED | None |
| No real fallback | Mock adapter rejects REAL and missing fixtures | mock adapter tests | COVERED | None |
| Publication order | Render/hash, artifact bound, JSON atomic replace, report atomic replace, trace | filesystem/fault tests | COVERED | None |
| Crash boundaries | Pre-authority retry may rerun; post-authority retry makes zero calls | parametrized publication fault tests | COVERED | None |
| Report reconciliation | Missing/corrupt report rebuilt without authority mutation/model call | reconciliation tests | COVERED | None |
| Deterministic output | Canonical JSON, UTF-8/LF, one final newline, exact SHA/order | deterministic publication test | COVERED | None |
| Same-ID replay | Authority loaded and report reconciled with zero calls | replay and completion-trace tests | COVERED | None |
| New verification CAS | Prior ID required; supersedes identity recorded | `test_same_id_reconcile_and_new_verification_cas` | FIXED | None |
| Trace safety | Only IDs/hashes/enums/counts/codes; no prose/content/locator/payload/secrets | trace leakage and provider-failure tests | COVERED | None |
| Phase 3 ownership regression | Executor, scheduler, checkpoint, and durable ledger behavior unchanged | Git diff inspection plus full regression suite | COVERED | None |
| Phase 5 semantic regression | Identity/revision contracts unchanged; only fingerprint reads added | Git diff inspection plus full Phase 5 suite | COVERED | None |
| No second runtime/retry/checkpoint/provider | Coordinator uses one typed port; no AgentRunner/Tool registry/retry/WAL/real adapter | source inspection | COVERED | None |
| Durable VERIFYING integration | Phase 3 remains sole durable lifecycle/budget owner | ADR-0022 | DEFERRED | Phase 8 integration hardening; does not change Phase 6 artifact semantics |
| Real provider integration | Provider-independent port and deterministic mock only | adapter inspection | DEFERRED | Phase 9; explicitly outside Phase 6 correctness |

## Severity result

- P0: 0
- P1: 0
- P2: 2 accepted deferrals shown above.
