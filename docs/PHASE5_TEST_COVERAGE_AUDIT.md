# Phase 5 Semantic and Test Coverage Audit

Date: 2026-08-28

Scope: Evidence Memory, Claim-Evidence Graph, Phase 4 observation ingestion,
local Phase 5 persistence, and structural citation integrity. Phase 6 truth
judgement, synthesis, and verification are excluded.

## Status summary

| Area | Status | Finding |
| --- | --- | --- |
| Claim and evidence identity | IMPLEMENTED | Stable scope keys define entities; mutable content and runtime occurrence identify revisions or receipts only. |
| Immutable revisions | IMPLEMENTED | Source/evidence, claim, and edge histories retain one validated current pointer. |
| Ingestion idempotency | IMPLEMENTED | Canonical logical request hashes exclude attempt and model Tool-call identity; occurrence provenance has separate receipts. |
| Extractor-specific scope | IMPLEMENTED | Browser page, local document/chunk, and search query/snippet scopes are deterministic and exclude rank/attempt/call identity. |
| Failed Agent attempts | IMPLEMENTED | Eligible successful observations are ingested before terminal Agent status mapping, including later decision and hard-limit failures. |
| Claim-evidence edge uniqueness | IMPLEMENTED | One edge per claim/evidence pair; relation changes are revisions of that edge. |
| Conflict candidates | IMPLEMENTED | Requires distinct current-valid supporting and contradicting evidence; contextual, stale, and tombstoned records are excluded. |
| Citation integrity | IMPLEMENTED | Structural errors are distinct from historical/tombstone warnings; duplicate citation identity conflicts are errors. |
| Persistence and crash boundaries | IMPLEMENTED | Deterministic guarded snapshots, CAS, atomic replace, corruption detection, and explicit replace/commit state are covered. |
| Query surface | IMPLEMENTED | Bounded deterministic current and exact-historical lookup plus forward/reverse relation traversal are covered. |
| Semantic trace safety | IMPLEMENTED | Phase 5 trace attributes contain identifiers, hashes, revisions, and dispositions, not raw payloads. |
| Durable multi-process coordination | INTENTIONALLY DEFERRED | Phase 5 is one application writer per run; leases and a database-backed transaction boundary are outside scope. |
| Truth/quality judgement and synthesis | INTENTIONALLY DEFERRED | Citation validation is structural only; truth scoring, verification, and synthesis belong to Phase 6. |

## Requirement-to-test mapping

| Requirement | Implementation | Test | Status | Gap |
| --- | --- | --- | --- | --- |
| Claim ID is stable across statement revisions | `ClaimGraphService.create_claim/revise_claim`; `ClaimRecord` | `test_claim_identity_is_scope_stable_and_statement_is_revisioned`; `test_claim_generation_context_revisions_do_not_change_logical_identity` | COVERED | None |
| Claim generation context is revision provenance, not identity | `ClaimGenerationContext`; `ClaimRevision.generation_context` | `test_claim_generation_context_revisions_do_not_change_logical_identity` | COVERED | None |
| Evidence identity excludes mutable content and runtime occurrence | `EvidenceExtractor`; `EvidenceMemory` | `test_browser_ingestion_and_runtime_occurrence_replay`; `test_changed_content_under_same_tool_operation_creates_revision` | COVERED | None |
| Conservative text normalization preserves meaningful internal whitespace | `text-nfc-lines-v1`; `normalized_content_hash` | `test_text_nfc_lines_normalization_is_conservative`; `test_internal_space_difference_is_not_a_normalized_duplicate` | COVERED | None |
| URL source identity preserves query order and encoding | `canonicalize_url` | `test_url_canonicalization_preserves_query_order_and_encoding` | COVERED | None |
| Exact normalized duplicates across sources remain distinct entities | duplicate query | `test_same_content_across_sources_is_classified_duplicate_not_merged` | COVERED | None |
| Retry occurrence fields do not affect canonical idempotency | stable ingestion payload and `IngestionReceipt` | `test_browser_ingestion_and_runtime_occurrence_replay`; `test_exact_occurrence_replay_does_not_mutate_state_and_is_distinct_trace`; `test_runner_backend_tool_to_evidence_retry_integration` | COVERED | None |
| Different content under one logical Tool operation becomes a revision | canonical content request hash and source/scope lineage | `test_changed_content_under_same_tool_operation_creates_revision` | COVERED | None |
| Browser/local/search scopes obey extractor rules | `EvidenceExtractor` | `test_extractor_specific_search_and_local_scopes_are_stable`; `test_search_scope_uses_query_context_but_not_rank_or_runtime_occurrence` | COVERED | None |
| Failed Agent result retains earlier eligible successful observations | `AgentRunner`; `AgentTaskExecutionBackend` | `test_backend_ingests_successful_observation_before_mapping_agent_failure`; `test_real_runner_failed_after_two_tools_still_ingests_both_observations` | COVERED | None |
| Backend observation ingestion order is adapter-independent | sort by `(agent_step, tool_call_id)` | `test_backend_ingests_observations_in_stable_agent_step_and_call_order` | COVERED | None |
| Successful observation survives later Agent hard-limit failure | `AgentRunner` observation capture before local terminal checks | `test_successful_tool_observation_survives_later_hard_limit_failure` | COVERED | None |
| Failed/cancelled/timed-out/Python/artifact-only results are not evidence | `EvidenceExtractor.is_eligible`; backend filter | `test_failed_tool_result_is_not_eligible`; `test_failed_agent_ingests_two_prior_successes_but_no_ineligible_results` | COVERED | None |
| Exactly one current edge per claim/evidence pair | `ClaimEvidenceEdge`; `ClaimGraphService.relate` | `test_relation_changes_revision_and_conflict_detection`; `test_edge_relation_revisions_keep_one_current_lineage` | COVERED | None |
| One relation alone never creates a conflict | `conflict_candidates` | `test_single_relation_never_forms_conflict_candidate` (all three relation types) | COVERED | None |
| Conflict requires distinct current-valid support and contradiction | current-valid edge filtering | `test_conflict_candidates_require_two_distinct_current_valid_edges`; `test_stale_pinned_claim_or_evidence_revision_is_not_current_conflict` | COVERED | None |
| Missing evidence cannot be related | claim graph evidence boundary | `test_relation_rejects_missing_evidence` | COVERED | None |
| Current and historical graph/evidence queries are bounded and deterministic | `get_*`, `list_*`, forward/reverse and relation queries | `test_evidence_query_is_bounded_and_trace_has_no_raw_payload`; `test_graph_queries_are_ordered_bounded_reverse_and_historical` | COVERED | None |
| Tombstones preserve history while default current queries exclude them | evidence/claim tombstone lifecycle | `test_graph_queries_are_ordered_bounded_reverse_and_historical`; conflict tombstone assertion in `test_conflict_candidates_require_two_distinct_current_valid_edges` | COVERED | None |
| Citation historical staleness is warning, not error | `CitationIntegrityValidator` | `test_citation_staleness_is_warning_and_missing_revision_is_error`; `test_citation_warnings_remain_valid_and_duplicate_id_conflict_is_error` | COVERED | None |
| Citation structural corruption is error | snapshot structural scan and reference validation | `test_citation_integrity_classifies_structural_corruption_as_errors` | COVERED | None |
| Citation pins source and expected evidence content | `CitationReference`; `CitationIntegrityValidator` | `test_citation_pins_source_and_evidence_content` | COVERED | None |
| Duplicate citation ID with conflicting target is error | citation-ID request validation | `test_citation_warnings_remain_valid_and_duplicate_id_conflict_is_error` | COVERED | None |
| Snapshot bytes/header/order are deterministic | `_snapshot_jsonl` and filesystem stores | `test_phase5_snapshot_bytes_are_deterministic`; `test_evidence_snapshot_record_order_is_canonical_not_caller_order` | COVERED | None |
| Before-replace failure publishes nothing and preserves old snapshot | atomic filesystem adapters | `test_atomic_failure_before_replace_never_publishes_snapshot`; `test_os_replace_failure_reports_not_replaced`; `test_failed_save_before_replace_preserves_old_authoritative_snapshot` | COVERED | None |
| After-replace failure exposes committed/replaced state | typed persistence errors | `test_filesystem_failure_exposes_replace_state`; `test_filesystem_claim_failure_exposes_replace_state`; `test_failure_after_parent_fsync_reports_replaced_and_new_snapshot_loads` | COVERED | None |
| Parent-directory fsync behavior is explicit on Windows | `_atomic_file.fsync_parent` | `test_windows_parent_directory_fsync_open_failure_is_best_effort` | COVERED | Best effort where directory handles do not support fsync |
| Non-Windows parent-directory fsync failures fail closed | `_atomic_file.fsync_parent` | `test_non_windows_parent_directory_fsync_open_failure_fails_closed` | COVERED | None |
| Torn, hash-corrupt, wrong-run, duplicate, or cross-reference-invalid snapshots fail closed | defensive store load/save validation | `test_filesystem_evidence_snapshot_roundtrip_and_corruption`; `test_evidence_snapshot_corruption_is_rejected`; `test_wrong_run_identity_is_rejected`; `test_duplicate_persisted_record_is_rejected_even_with_matching_envelope_hash`; `test_store_revalidates_current_pointer_and_cross_references` | COVERED | None |
| Unsafe persistent models are rejected, not silently redacted | store persistence-safety boundary | `test_malformed_content_hash_and_secret_content_are_not_persisted`; `test_filesystem_store_rejects_model_that_requires_redaction` | COVERED | None |
| Store commit followed by trace failure is explicit and replayable | typed `store_committed`; receipts | `test_trace_failure_reports_already_committed_store`; `test_claim_revision_receipt_replays_after_terminal_trace_failure`; `test_evidence_trace_failure_retry_replays_receipt_without_new_revision` | COVERED | General trace reconciliation remains deferred |
| Entity/store revision CAS rejects stale writers | claim entity expected revision; store expected revision | `test_claim_entity_revision_compare_and_swap_rejects_stale_writer`; `test_filesystem_evidence_compare_and_swap` | COVERED | Multi-process leases intentionally deferred |
| Revision chains, current pointers, receipt identities, and receipt targets fail closed | snapshot model validators | `test_evidence_snapshot_rejects_missing_middle_revision`; `test_evidence_snapshot_rejects_current_pointer_below_maximum`; `test_evidence_snapshot_rejects_duplicate_receipt_identity`; `test_evidence_snapshot_rejects_receipt_with_missing_revision`; `test_claim_snapshot_rejects_missing_middle_revision`; `test_claim_snapshot_rejects_current_pointer_below_maximum`; `test_edge_revision_rejects_missing_claim_revision_pin`; `test_claim_snapshot_rejects_duplicate_receipt`; `test_claim_snapshot_rejects_ambiguous_mutation_operation` | COVERED | Cross-store evidence pins remain joint boundary checks |
| Repeated claim creation appends changed statements to one identity | `ClaimGraphService.create_claim` | `test_create_claim_same_scope_changed_statement_appends_revision` | COVERED | None |
| Phase 5 traces do not leak raw claim/evidence/source text | safe semantic event attributes | `test_evidence_query_is_bounded_and_trace_has_no_raw_payload`; `test_phase5_trace_attributes_exclude_raw_claim_evidence_and_source_text` | COVERED | None |

## ADR audit

ADR-0021 intentionally consolidates the three originally planned records:

1. stable identity, immutable revisions, edge uniqueness, and occurrence receipts;
2. deterministic snapshot persistence, CAS, corruption handling, and atomic replace;
3. Agent observation ingestion ordering, state-before-trace failure semantics, replay, and trace safety.

The concerns use one canonical identity/revision/receipt invariant and have no
independent contradictory operating mode, so one ADR is clearer than three
cross-dependent records. `docs/DECISIONS.md` now states this consolidation
explicitly.

## P0/P1/P2 self-review

- P0: none found. Stores fail closed on structural corruption and unsafe
  persistence data; no silent mock fallback or fabricated provider exists.
- P1 fixed: current conflict detection previously admitted edges pinned to
  stale claim/evidence revisions. It now requires current entity revisions and
  active records.
- P1 fixed: a successful validated Tool observation could be omitted when its
  actual usage subsequently caused Agent hard-limit failure. Observation
  capture now precedes that terminal failure check, preserving honest usage and
  eligible evidence.
- P1 fixed: persistence loaders/savers now defensively revalidate derived IDs,
  hashes, current pointers, cross-record references, and duplicate records.
- P2 fixed locally: bounded deterministic query coverage, exact historical
  lookup, reverse traversal, relation filters, tombstone behavior, claim
  generation provenance, and duplicate citation-ID handling were made explicit.
- Accepted risk: current-only keyset pagination uses stable entity/edge IDs.
  Exact historical revision lookup is supported by `get_*`; enumerating all
  historical revisions with a compound cursor is not a Phase 5 requirement.

## Deferred boundaries

- No Phase 3 checkpoint/outbox ownership was changed.
- No cross-store transaction, database, generic WAL, event sourcing, lease, or
  multi-process writer protocol was added.
- No citation truth assessment, evidence quality score, synthesis, Red/Blue/Judge,
  or evaluation runtime was added.
- No real browser/search/LLM provider was added.
