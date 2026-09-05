# Phase 10 — End-to-End Research Workflow

## Current-state audit

- `RunManager` already owns the only legal lifecycle transitions and durable state/trace reconciliation. `AsyncDAGExecutor` owns task scheduling, checkpointing, retries, cancellation, and the runtime budget ledger.
- `tests/test_phase9_offline_e2e.py` proves the desired chain, but manually assembles Planner, Executor, Evidence, ClaimGraph, Verification, and Evaluation.
- Evidence ingestion is already automatic through `AgentTaskExecutionBackend -> EvidenceMemory`; Claim creation is not—tests call `ClaimGraphService.create_claim()` directly.
- Phase 6 durable verification already owns `RUNNING -> VERIFYING -> EVALUATING`, authority-first replay, model-call journal, and report publication. Phase 7 evaluation is read-only and authority-idempotent.
- Current CLI only exposes `doctor`. REAL composition has Planning/Agent/Verification roles and exact capability/model pins.
- Important durability gap: a validated plan is not independently persisted before the runtime checkpoint exists. Phase 10 must close this handoff without creating another runtime or changing Phase 9 schema-v1 checkpoint/claim bytes.

## Architecture decision

Add a thin `WorkflowCoordinator` application service plus a mode-specific factory. It orchestrates existing components; it never writes `RunState` directly, schedules tasks, owns a mutable budget ledger, or writes existing authority JSON.

The durable continuation source is always existing authority:

| Durable fact | Owner / use |
| --- | --- |
| Run lifecycle | `RunManager` / legal status |
| Validated DAG, execution policy, task outcomes, replan lineage | Phase 10 handoff until the normal Phase 3 checkpoint exists; then only the existing Phase 3 checkpoint |
| Evidence | `EvidenceMemory` / existing evidence store |
| Claims, edges, and receipts | `ClaimGraphService` / existing ClaimGraph authority |
| Claim-extraction dispatch/recovery state | separately versioned Phase 10 `ClaimExtractionOperation` journal only |
| Verification and model-call replay | existing Phase 8 operation plus Phase 6 authority |
| Evaluation | existing Phase 7 authority |

### Separate workflow handoff authority

`RuntimeCheckpoint` schema v1, its canonical bytes, and its Phase 9 fixture hashes remain unchanged. Phase 10 adds a separate, versioned `WorkflowRuntimeHandoff` artifact and narrow store, for example `outputs/<run_id>/workflow_runtime_handoff.json`.

The artifact is the only durable bridge from a validated `PLANNING` result to creation of the normal Phase 3 checkpoint. Its immutable semantic payload pins:

- `run_id` and the expected Run revision(s) for the handoff;
- the validated DAG and DAG hash;
- the execution policy and policy hash;
- the trusted replan context;
- the immutable `WorkflowBudgetAllocation` and its canonical hash;
- schema/version, identity, and canonical semantic hash.

The handoff store uses the repository's existing atomic-write/CAS discipline. Its minimal state is `PREPARED` or `CHECKPOINT_COMMITTED`; it contains no task states, attempts, mutable budget consumption, scheduling cursor, outbox, or task outcome. It therefore is not a second runtime/checkpoint/task-state authority.

Sequence:

1. In `PLANNING`, validate the planner result and write the fully pinned `PREPARED` handoff.
2. Use `RunManager` for the legal `PLANNING -> READY -> RUNNING` transitions.
3. Create the ordinary Phase 3 checkpoint from the persisted handoff, using its execution budget slice.
4. Mark the handoff `CHECKPOINT_COMMITTED` only after the checkpoint exists and exactly matches all pins. If a crash occurs after checkpoint persistence and before this mark, recovery verifies the checkpoint and completes the handoff marker without recreating it.

On recovery, a valid `PREPARED` handoff may only create or validate that one normal checkpoint. Once a matching Phase 3 checkpoint exists, the executor checkpoint is the sole runtime authority and the handoff is never used to infer/rewrite task state. A missing or mismatched handoff/checkpoint at a state that requires it is typed corruption/failure, never an in-memory-cursor fallback.

This is a narrow bridge, not an extension of the existing checkpoint/outbox protocol and not a second checkpoint system. A fresh process derives continuation from persisted Run state plus the handoff before checkpoint creation, and from the normal Phase 3 checkpoint after it exists; it never relies on a process-local stage cursor.

### Immutable workflow budget allocation

The handoff contains one immutable `WorkflowBudgetAllocation`:

```text
total
planning
execution
claim_extraction
verification
```

Every slice is a resource vector for duration, tokens, cost, and tool calls. Per resource dimension all values are non-negative and the four slices exactly partition `total`; `total` must equal the effective Run budget frozen at workflow creation. Slices are consequently bounded, non-overlapping, and cannot be reassigned after the handoff is prepared.

- The coordinator passes `PerspectivePlanner` a narrow explicit remaining-budget override/view, consumed by `_new_request()`, whose `PlanningRequest.remaining_budget` is exactly the `execution` slice. It is ephemeral and never persisted as a rewritten Run budget, so planned DAG feasibility cannot consume the tail envelopes.
- The existing Phase 3 executor is initialized with the `execution` slice as the `RuntimeBudgetState` limits. A narrow explicit initialization override preserves existing callers' full-Run-budget behavior and persists the selected limits in the already-existing checkpoint budget field; it does not change checkpoint schema v1.
- The one bounded Claim Extraction model invocation must pass existing provider admission against `claim_extraction`; its immutable provider upper bound and every known settlement must fit that slice.
- Existing durable verification receives hard limits bounded by `verification`.
- Before any REAL planning HTTP dispatch, the existing immutable planning provider-call reservation must fit `planning`; otherwise the coordinator fails closed before dispatch. `planning` is not transferable to another stage.

No Phase 10 component creates a second mutable budget ledger. Phase 3 remains the only mutable execution ledger. Claim extraction and verification keep their already-required operation usage/certainty records only for their own bounded calls; `EXACT`, `UPPER_BOUND`, and `UNKNOWN` semantics are preserved, and UNKNOWN dispatched provider consumption is never released or reallocated.

## Lifecycle and resume semantics

| Persisted state | Coordinator action |
| --- | --- |
| `CREATED` | Safely begin `PLANNING`. |
| `PLANNING`, no `planning.started` trace | Planning was not dispatched; invoke Planner once. |
| `PLANNING`, planning started but no valid workflow handoff | Fail closed as `FAILED` for an unknown/incomplete planner outcome with no usable research result; do not re-call the model. |
| `PLANNING`/`READY`, valid `PREPARED` handoff exists | Validate all Run/DAG/policy/revision/allocation pins, advance only the missing legal lifecycle transitions, create or validate the normal Phase 3 checkpoint, then execute. |
| `RUNNING`, valid handoff but no checkpoint | Create the one pinned Phase 3 checkpoint; no task outcome is inferred. |
| `RUNNING` | Resume existing executor checkpoint. Completed task effects are not replayed. A paused checkpoint remains a surfaced `PARTIAL`; Phase 10 does not implement new runtime-replan application semantics. |
| `VERIFYING` | Call existing `DurableVerificationCoordinator.recover()`. |
| `EVALUATING` | Reconstruct the deterministic structural evaluation request from persisted Run input/config and read-only artifacts; same-ID evaluation replay performs zero evaluator/model calls. |
| terminal | Return the durable result; no provider call. |

Missing, mismatched, torn, or corrupt handoff/checkpoint/claim/verification/evaluation authority raises a typed recovery/corruption error and performs no lifecycle guesswork. REAL composition mismatch, missing optional dependency, or missing claim-extraction role fails closed; none may downgrade to MOCK or non-durable execution.

Terminal mapping is fixed:

- `COMPLETED`: all tasks succeeded, Claim Extraction completed, verification is `VERIFIED`, and Evaluation completed.
- `PARTIAL`: safely terminal execution with failed/blocked work, no eligible evidence, known Claim Extraction failure/malformed result/unknown dispatch, non-verified verification, or a published partial/failed evaluation.
- `FAILED`: planner failure/invalid plan, persistence/contract/configuration failure, or corruption before a usable research result exists.
- `CANCELLED`: cancellation at any active stage; later stages never start.

## Claim Extraction

Add a bounded, provider-independent `ClaimExtractor` with strict Pydantic contracts:

- `ClaimExtractionPolicy`: explicit evidence/claim/reference/context/response byte limits; exactly one model invocation; no retry.
- `ClaimExtractionRequest`: run ID/revision, extraction ID, canonical request hash, evidence snapshot hash, frozen current evidence revision pins, and bounded whole-item evidence context. Items that do not fit are omitted as complete items and their IDs are recorded; content is never truncated under its original hash.
- `ClaimExtractionResponse`: ordered `ClaimCandidate`s, each with immutable ordinal, statement, and explicit evidence revision pins with `SUPPORTS`, `CONTRADICTS`, or `CONTEXTUALIZES`.
- `claim_scope_key = stable_id("scope", [claim_extraction_id, ordinal])`; mutable prose never determines claim identity.
- Every pin must belong to the frozen current Run evidence snapshot; duplicate pins, cross-Run IDs, nonexistent revisions, no `SUPPORTS` pin, malformed structure, excess bounds, or unsafe persistent data are rejected before any Claim mutation.

`ClaimGraphSnapshot`, `claims.jsonl`, and all Phase 9 ClaimGraph hashes remain schema-v1 artifacts and are not extended. Phase 10 instead adds a separately versioned `ClaimExtractionOperation` artifact/store/manager, analogous to the durable VerificationOperation journal. It owns only safe model-dispatch and recovery state: `PREPARED`, `DISPATCHED`, `VALIDATED`, `COMPLETED`, and typed terminal failure/cancellation/`INTERRUPTED_UNKNOWN` states. It stores safe request/model/policy/input hashes, the validated typed response, usage certainty, and deterministic mutation keys—never raw prompt/provider payload and never ClaimGraph records.

Sequence:

1. Freeze and hash eligible active Evidence.
2. Create/reload the separate operation artifact.
3. Persist `DISPATCHED` before model invocation.
4. Validate the strict response and persist `VALIDATED`.
5. Replay deterministic `ClaimGraphService.create_claim()` and `.relate()` keys until every expected ClaimGraph receipt exists.
6. Mark the operation `COMPLETED`.

`ClaimGraphService` remains the sole Claim/Edge/Receipt authority. A `VALIDATED` operation survives restart and replays only deterministic, idempotent ClaimGraph mutations without another model call. `DISPATCHED` without a durable validated response becomes `INTERRUPTED_UNKNOWN`; no transparent retry occurs. Existing Claims/edges are reused through operation keys and receipts, never duplicated.

MODEL-backed extraction is mandatory for Phase 10:

- Add provider-independent `ClaimExtractionModel`, deterministic exact-fixture `MockClaimExtractionModel`, and `DeepSeekClaimExtractionModel`.
- REAL adds an explicit `claim_extraction` model role only for the Phase 10 workflow profile. Legacy three-role Phase 9 compositions remain valid for historical Phase 9 behavior, but cannot enter Phase 10 extraction; they fail closed.
- The REAL adapter uses current composition binding, dispatch authorizer, credential source, immutable model bundle/policy, transport timeout, cancellation, response-size bound, usage certainty, and reservation admission.
- The `RunLifecycleDispatchAuthorizer` adds an explicit `claim_extraction` REAL role, accepted only while the Run is `RUNNING`. Before HTTP dispatch, its immutable provider-call reservation must fit the handoff's `claim_extraction` slice; failure rejects before dispatch. Planner DAG validation sees only the separate `execution` slice and verification receives only its own bounded slice. Phase 10 does not create a ledger or silently estimate provider cost.

## CLI, output, and inspection

Add:

```text
researchos run "question" --mode mock|real ...
researchos resume <run_id> ...
researchos inspect <run_id>
```

- `run` constructs only the official filesystem-backed workflow factory. MOCK requires an explicit versioned fixture bundle; REAL derives capabilities and immutable composition from current REAL settings. No fixture/configuration means no run.
- `resume` loads persisted input/config and calls `RunManager.resume`. If the stored input was redacted, `--question` is required to re-prove the input hash. REAL also revalidates the persisted composition against current settings before any credential read or provider call.
- `inspect` is a read-only projection: `RunManager.load`, non-reconciling trace/checkpoint/handoff/ClaimExtractionOperation reads, and read-only Evidence/Claim/Verification/Evaluation loads only. It reports lifecycle, runtime task counts, Claim Extraction state, and authority presence/corruption; it never repairs a torn trace, writes an event, reads credentials, or calls a provider.
- The CLI summary is safe JSON. Existing filesystem authority layout remains canonical; the two narrow Phase 10 bridge/journal artifacts do not duplicate Run, runtime task, ClaimGraph, Verification, or Evaluation authority. Finalization records existing artifact paths/hashes in `RunState.artifacts`.

## External pattern comparison

- **LangGraph:** adopt durable boundary discipline, persisted side-effect identity, and resume from recorded state—not replay from a process cursor. Do not add LangGraph, graph replay, interrupts, or its checkpoint store; ResearchOS retains its existing checkpoint/outbox protocol. [LangGraph functional durability guidance](https://docs.langchain.com/oss/python/langgraph/functional-api)
- **Pydantic AI:** adopt typed node/input/output boundaries. Do not add Pydantic Graph, Temporal, DBOS, Prefect, or Restate; those would replace rather than compose with the current Runtime. [Pydantic Graph typed node contracts](https://ai.pydantic.dev/api/pydantic_graph/beta_node/)
- **OpenAI Agents SDK:** adopt strict structured output plus pre-persistence validation guardrails, and preserve trace/export separation from workflow authority. Do not add handoffs or agents-as-tools; DAG tasks and CapabilityRegistry remain the delegation/permission boundary. [Structured output](https://openai.github.io/openai-agents-python/ref/agent/), [guardrails](https://openai.github.io/openai-agents-python/guardrails/)
- **DeepAgents:** adopt the distinction between middleware-like cross-cutting validation and Tool authorization. Do not add middleware stacks, subagents, filesystem backends, or HITL checkpoints; existing Tool/Registry/Phase 3 ownership is narrower and already durable. [DeepAgents boundary design](https://github.com/langchain-ai/deepagents/blob/main/openwiki/architecture/overview.md)
- **AutoGen / ADK:** adopt explicit serializable state and save/load discipline. Do not persist agent conversation/session state or sandbox state; ResearchOS resumes from Run/handoff/checkpoint/evidence/claim/verification/evaluation authority instead. [AutoGen save/load state](https://microsoft.github.io/autogen/dev/user-guide/agentchat-user-guide/tutorial/state.html)

## Implementation changes

Add:

- `domain/workflow.py` for workflow request/policy/result/inspection, `WorkflowBudgetAllocation`, and the separately versioned `WorkflowRuntimeHandoff` contract; `interfaces/workflow_handoff.py` plus filesystem/in-memory handoff stores.
- `domain/claim_extraction.py`, `domain/claim_extraction_operation.py`, `interfaces/claim_extraction.py`, and filesystem/in-memory Claim Extraction operation stores for strict extraction contracts and the separate journal.
- `application/workflow_coordinator.py`, `application/workflow_factory.py`, `application/claim_extractor.py`, and `application/claim_extraction_operation_manager.py`.
- `adapters/mock_claim_extraction.py` and Phase 10 CLI fixture-bundle loader.

Modify only required compatibility boundaries:

- `application/async_dag_executor.py` and executor initialization tests for the explicit immutable execution-budget-limits input. Existing callers retain the current full-Run-budget default; the existing checkpoint schema and Phase 9 v1 fixture bytes/hashes do not change.
- `application/provider_dispatch.py` (or the existing narrow planning admission boundary) and the minimal compatible `PerspectivePlanner` request construction boundary for planning-slice admission and the explicit execution-slice remaining-budget override. Historical Phase 9 planning, Agent, and Verification paths retain their current behavior.
- `application/claim_graph.py` only for idempotent application/query of existing ClaimGraph mutation receipts; do not modify `domain/claims.py`, `ClaimGraphSnapshot`, or `claims.jsonl` schema/persistence.
- `adapters/deepseek.py`, `application/integration_factory.py`, `configuration/real_settings.py`, `configuration/environment.py`, and validation contracts for the explicit `claim_extraction` REAL role.
- `cli.py` for thin `run`, `resume`, and `inspect`.
- `docs/PHASE10_DESIGN.md`, `docs/DECISIONS.md` (new ADR), `docs/TASKS.md`, `README.md`, `docs/ARCHITECTURE.md`, and `docs/OPERATOR_RUNBOOK.md`.

## Test plan and acceptance criteria

Add deterministic offline tests for:

- separate workflow-handoff CAS and crash boundaries before/after `READY`, `RUNNING`, and normal checkpoint persistence; two writers at revision N permit only one N+1 commit; fresh-process continuation validates pins and has no local cursor;
- frozen Phase 9 schema-v1 checkpoint and ClaimGraph corpus compatibility: existing `checkpoint.json` and `claims.jsonl` canonical bytes/hashes load unchanged, while unknown/missing/tampered handoff or operation artifacts fail closed;
- strict Claim Extraction validation, full-item admission, malformed/missing fixture, cross-Run Evidence, duplicate pin, unsupported claim, safe redaction, bound enforcement, and no raw payload persistence;
- separate extraction operation replay: `VALIDATED` response replays ClaimGraph writes once; `DISPATCHED`/unknown outcome never retries; partial graph mutation resumes idempotently; ClaimGraph files/hashes contain only ClaimGraph authority;
- allocation partition validation; planner uses only the execution slice; runtime `RuntimeBudgetState` uses only the execution slice; Claim Extraction and Verification reject admission/limits that exceed their respective slices; `EXACT`/`UPPER_BOUND`/`UNKNOWN` usage retains existing semantics and UNKNOWN is never released or moved between slices;
- provider-boundary slice admission: an initial planning reservation exceeding `planning` makes zero provider calls; `PlanningRequest.remaining_budget` exactly equals `execution`; `claim_extraction` is rejected outside `RUNNING`; its over-slice reservation makes zero provider calls; legacy Phase 9 planning/Agent/Verification behavior is unchanged;
- planner failure, task partial, no Evidence, Claim Extraction failure, verification interruption, evaluation failure, cancellation, and exact terminal mapping;
- formal filesystem MOCK workflow and CLI path through report/Evaluation/`COMPLETED`, then fresh factory reload with no duplicate authority;
- `resume` at every supported durable stage, redacted-input requirement, REAL composition mismatch, missing claim role, and absent optional extras;
- `inspect` byte-for-byte read-only behavior;
- all Phase 0–9 regression suites, core CI, all-extras offline CI, Ruff, diff check, build, and clean wheel install. No paid REAL call is run automatically.

Phase 10 is done only when the formal CLI exercises the complete deterministic MOCK workflow, filesystem restart proves authority-idempotent continuation, budget slices are actually enforced at their existing owners, no legacy authority is reinterpreted, and all release gates pass.

## Explicit non-goals

No frontend/server, new provider/search/vector database, retrieval redesign, reranker, benchmark, REAL E2E benchmark, migration framework, general workflow engine, generic event sourcing/WAL, external durable runtime, second scheduler/budget/checkpoint/trace authority, automatic paid calls, or Phase 1–9 semantic redesign.
