# Phase 4 Test Coverage Audit

**Audit date:** 2026-08-28  
**Baseline:** 322 tests after Phase 3; 336 tests before this audit.

The mappings below identify executable behavior tests, not merely schema or
line coverage. Parameterized tests count as separate collected cases.

## Agent and AgentRunner

| Required semantic | Test mapping |
|---|---|
| Direct final | `test_agent_direct_final_has_ordered_trace` |
| Tool then final and structured observation | `test_agent_tool_observation_then_final_is_structured` |
| Malformed decision | `test_malformed_decision_fails_without_payload_leakage` |
| Max steps | `test_agent_failure_and_max_steps_are_distinct` |
| Agent-declared failure | `test_agent_failure_and_max_steps_are_distinct` |
| Cancellation during decision | `test_cancellation_interrupts_agent_decision` |
| Deadline during a decision that would return | `test_deadline_interrupts_a_decision_that_would_eventually_return` |
| Never-return | `test_agent_never_returns_is_bounded_by_deadline` |

## Capability registry and modes

| Required semantic | Test mapping |
|---|---|
| Duplicate capability and duplicate Tool ID | `test_registry_rejects_duplicate_and_unknown_entries` |
| Unknown capability | `test_registry_rejects_duplicate_and_unknown_entries` |
| Unauthorized capability and default deny | `test_registry_presence_does_not_grant_permission` |
| Adapter mode mismatch | `test_registry_rejects_mode_without_fallback`, `test_runner_rejects_agent_mode_without_fallback` |
| Mock cannot represent REAL; no fallback | `test_mock_adapters_cannot_claim_real_mode` |

## Tool identity and usage

| Required semantic | Test mapping |
|---|---|
| Same logical call across task retry | `test_phase3_owns_retry_and_tool_identity_survives_attempt_change` |
| Different attempt key does not affect Tool key | `test_tool_operation_key_ignores_attempt_and_tool_call_identity` |
| Input/version/adapter changes affect Tool key | `test_tool_operation_key_changes_with_input_version_and_adapter` |
| `tool_call_id` does not affect Tool key | `test_tool_operation_key_ignores_attempt_and_tool_call_identity` |
| EXACT and UPPER_BOUND aggregation | `test_exact_and_upper_bound_usage_aggregate_without_second_ledger` |
| UNKNOWN propagation | `test_unknown_tool_usage_propagates_without_amount`, `test_tool_never_returns_is_bounded_and_usage_becomes_unknown` |
| Token/cost/Tool-call/duration hard limits | parameterized `test_each_hard_limit_preserves_known_overrun` |
| Honest unclamped overrun | `test_known_tool_overrun_is_not_clamped`, `test_each_hard_limit_preserves_known_overrun` |

## Trace

| Required semantic | Test mapping |
|---|---|
| Agent start/decision/terminal order | `test_agent_direct_final_has_ordered_trace` |
| Tool requested/start/terminal order and causation | `test_agent_tool_observation_then_final_is_structured` |
| Terminal append failure | `test_terminal_trace_retry_requires_both_idempotent_layers` |
| Task + Tool + failure-policy retryability | parameterized `test_terminal_trace_retry_requires_both_idempotent_layers`, `test_terminal_trace_retry_respects_failure_policy` |
| No raw input/output/source/environment/secret persistence | `test_agent_tool_observation_then_final_is_structured`, `test_trace_excludes_raw_tool_output_provenance_and_secret`, `test_malformed_decision_fails_without_payload_leakage`, `test_python_subprocess_timeout_is_enforced_by_agent_runner` |

## Local retrieval

| Required semantic | Test mapping |
|---|---|
| Deterministic BM25 ranking and tie break | `test_local_retrieval_tie_break_empty_duplicate_and_content_hash` |
| Empty results | `test_local_retrieval_tie_break_empty_duplicate_and_content_hash` |
| Malformed JSONL | `test_local_retrieval_is_deterministic_and_rejects_malformed` |
| Duplicate chunk | `test_local_retrieval_tie_break_empty_duplicate_and_content_hash` |
| Traversal and symlink escape | `test_local_retrieval_rejects_traversal_and_symlink_escape` |
| Content hash and source metadata | `test_local_retrieval_tie_break_empty_duplicate_and_content_hash` |

## Python subprocess and artifacts

| Required semantic | Test mapping |
|---|---|
| Success and declared artifact | `test_python_subprocess_publishes_declared_artifact` |
| Syntax error and unsupported import | `test_python_syntax_runtime_and_output_limits`, `test_python_subprocess_publishes_declared_artifact` |
| Runtime error | `test_python_syntax_runtime_and_output_limits` |
| Timeout and child cleanup | `test_python_subprocess_timeout_is_enforced_by_agent_runner` |
| Cancellation and child cleanup | `test_python_cancellation_terminates_child` |
| stdout/stderr bounds | `test_python_syntax_runtime_and_output_limits` |
| Artifact count/size limits and path traversal | `test_python_artifact_count_size_and_path_limits` |
| Sanitized environment, disabled stdin, argv execution, no shell | `test_python_environment_stdin_and_exec_shape_are_restricted` |
| Atomic artifact write and idempotence/conflict | `test_filesystem_artifact_store_is_idempotent_and_detects_conflict`; the shared atomic primitive retains its Phase 1 fault-injection coverage |
| Artifact traversal and symlink escape | `test_filesystem_artifact_store_rejects_traversal_and_symlink_escape` |

## Browser and Search

| Required semantic | Test mapping |
|---|---|
| Contract URL/result validation | `test_browser_search_contracts_and_exact_mock_fixture` |
| Exact fixture and empty result | `test_browser_search_contracts_and_exact_mock_fixture` |
| Missing fixture | `test_browser_search_contracts_and_exact_mock_fixture` |
| REAL unavailable and no mock fallback | `test_mock_adapters_cannot_claim_real_mode`, `test_registry_rejects_mode_without_fallback` |

## Phase 3 integration

All integration tests traverse `AsyncDAGExecutor` →
`AgentTaskExecutionBackend` → `AgentRunner` and, where applicable, `Tool` →
`TaskExecutionResult`.

| Required semantic | Test mapping |
|---|---|
| Success | `test_phase3_agent_tool_chain_success` |
| Failure | `test_phase3_agent_failure_and_expected_output_mismatch` |
| Retry owned by Phase 3 | `test_phase3_owns_retry_and_tool_identity_survives_attempt_change` |
| UNKNOWN usage settlement | `test_phase3_settles_unknown_agent_tool_usage_conservatively` |
| Cancellation | `test_phase3_cancellation_reaches_agent_decisions` |
| Expected output ID mismatch | `test_phase3_agent_failure_and_expected_output_mismatch` |

## Audit outcome

Before the audit, several listed semantics were only implicit or shared one
assertion. The added tests independently exercise the public contracts and the
full Phase 3/4 chain. The audit also exposed and fixed a real cancellation
cleanup race. No duplicate count-only tests, real providers, Phase 5 contracts,
or alternative retry/budget systems were added.
