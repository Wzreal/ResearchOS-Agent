# ResearchOS Phase 10 开发交接

> **已完成（2026-09-05）**：以下内容保留为实施前交接历史。官方
> `phase10_mock@1`、`WorkflowFactory`、`WorkflowCoordinator`、CLI、fresh-process
> terminal idempotency、nonterminal fresh-process recovery、严格只读 inspect 和
> `structural_selfcheck_v1` 已实现并完成
> 离线验证。缺少显式 Phase 10 REAL 配置时 `researchos run --mode real` fail
> closed；本阶段未进行 REAL/provider 调用。以 README、TASKS、DECISIONS 和
> OPERATOR_RUNBOOK 的当前说明为准；不启动 Phase 11。

## 1. 当前工作区

- 仓库：`E:\ResearchOS Agent`
- 分支：`feat/phase-10-e2e-workflow`
- 当前 HEAD：`4512f09b239ea5967a3f1a284b12ad774925d9b3`
- 工作树包含大量未提交修改和未跟踪的新文件；不要 reset、checkout、clean、覆盖或丢弃。
- 本窗口没有 commit、push 或 REAL/付费网络调用。
- `PLAN10.md` 是未跟踪文件，但包含已批准的 Phase 10 总体设计；继续前先完整阅读。

## 2. 不可改变的边界

- 不修改已冻结的 `RunConfig v1`、`RuntimeCheckpoint v1`、`ClaimGraphSnapshot v1` 和 Phase 1–9 历史语义。
- 不增加第二套 lifecycle、scheduler、budget ledger、checkpoint、Evidence、Claim、Verification 或 Evaluation authority。
- Phase 10 新增且尚未发布的 contract/API 可以直接完善 v1；不为 draft Phase 10 artifact 建 schema v2 或 migration。
- MOCK/REAL 必须显式，无 fallback；禁止 REAL/付费调用。
- `WorkflowCoordinator` 不得保存 process-local stage cursor，必须从 RunState、trace、handoff、checkpoint、Evidence、Claim Extraction operation、ClaimGraph、Verification、Evaluation authorities 推导 continuation。
- `inspect` 必须严格只读：零写入、零 mkdir、零 repair/reconciliation、零 credential read、零 provider/network call。

## 3. 已批准的 Phase 10 workflow profile

`Phase10WorkflowProfileV1` 是显式配置，不是 runtime authority。它应完整包含：

- workflow budget config/allocation；
- `PlanningPolicy`；
- `Phase10ExecutionPolicyConfigV1`；
- `ClaimExtractionPolicy`；
- `VerificationPolicy`；
- `Phase10EvaluationProfileV1`；
- mandatory declared `system_commit_sha`；
- mandatory declared `system_version`。

Profile 的 canonical hash 必须同时持久化于：

- `PLANNING_STARTED.attributes.workflow_profile_hash`；
- `WorkflowRuntimeHandoff.workflow_profile_hash`。

恢复时二者和当前显式 profile 必须完全一致，否则 fail closed。

Phase 10 Evaluation 唯一模式是 `structural_selfcheck_v1`：由持久化 Run input 与 pinned workflow profile 确定性构造一个 `ReferenceLevel.STRUCTURAL_ONLY` case，通过现有 `InMemoryEvaluationDatasetLoader((dataset,))` 交给未修改的 Phase 7 `EvaluationHarness`。它不是 benchmark、事实质量评价或 REAL quality measurement。

## 4. 当前已实现代码

### 4.1 Planning / budget freeze

- `PerspectivePlanner.plan()` 已增加可选 Phase 10 admission 参数，旧 Phase 2 caller 保持原路径。
- `PlanningRequest.remaining_budget` 在 Phase 10 路径使用 immutable execution slice。
- admission 在 `PLANNING_STARTED` 前验证 planning reservation；trace append 失败时 model 不会被调用。
- trace safe attributes 已包含 allocation、allocation hash、planning reservation/hash、budget profile/version/hash、workflow profile hash。
- `AsyncDAGExecutor.initialize()` 已支持显式 execution budget limits；默认路径仍使用旧 Run budget。

### 4.2 Execution policy / handoff

- `ExecutionPolicyBuilder` 已存在，显式接收 validated DAG、execution slice 和 `Phase10ExecutionPolicyConfigV1`。
- `WorkflowRuntimeHandoff` 是独立 Phase 10 artifact，不修改 RuntimeCheckpoint v1。
- memory/filesystem handoff store 已存在，并实现 atomic/CAS persistence。
- `WorkflowHandoffManager` 已实现 validated planning result 到 Phase 3 checkpoint 的 bridge。
- handoff v1 已新增 mandatory `workflow_profile_hash`，并纳入 semantic hash preimage。

### 4.3 Claim Extraction

- strict contracts：policy/request/response/candidate/evidence revision pin。
- 独立 `ClaimExtractionOperation` journal：`PREPARED`、`DISPATCHED`、`VALIDATED`、`COMPLETED`、`FAILED`、`CANCELLED`、`INTERRUPTED_UNKNOWN`。
- memory/filesystem CAS stores 与 operation manager 已存在。
- `ClaimExtractor` 已实现 Evidence freeze、一次 model dispatch、strict validation、VALIDATED-before-graph、receipt-driven ClaimGraph replay。
- `DISPATCHED` restart 转 `INTERRUPTED_UNKNOWN`，不自动重调模型；`VALIDATED` restart 只 replay graph。
- `ClaimGraphService.mutation_receipt()` 只读查询已加入；ClaimGraph schema v1 未增加 operation record。
- 已修复状态机缺口：model dispatch 后 validation/model failure 允许 `DISPATCHED -> FAILED`。
- `MockClaimExtractionModel` exact fixture only，有 invocation counter。

### 4.4 REAL Claim Extraction boundary

- DeepSeek strict prompt/response schema 已加入 `claim_extraction` role。
- `RunLifecycleDispatchAuthorizer` 只允许该 role 在 `RUNNING`。
- `DeepSeekClaimExtractionModel` 使用现有 authorizer、composition binding、transport 与 usage/error boundary。
- provider reservation 在 transport 前检查 immutable claim-extraction slice。
- `RealCompositionManager(require_phase10_roles=True)` 可要求 planning/agent/verification/claim_extraction 四角色；默认 Phase 9 三角色规则保留。
- `RealIntegrationFactory.claim_extraction_model()` 已加入，但尚未由官方 Phase 10 factory 调用。

### 4.5 Structural self-check helper

- `workflow_evaluation.py` 已有 dataset、request 和 existing in-memory loader 的构造 helper。
- request 使用 workflow profile 中 declared `system_commit_sha`/`system_version`。

## 5. 当前明确未完成

以下不是 blocker，而是新窗口必须继续实现的工作：

1. `WorkflowCoordinator` 尚不存在。
2. 官方 filesystem-backed `WorkflowFactory` 尚不存在。
3. `researchos run/resume/inspect` 尚未加入；当前 CLI 只有 Phase 9 `doctor`。
4. 正式 deterministic MOCK E2E 尚不存在。
5. Phase 10 full recovery/failure matrix 尚未完成。
6. README、TASKS、DECISIONS、Phase 10 design、OPERATOR_RUNBOOK 尚未按实际实现更新。
7. 尚未运行 full core/all-extras suite、`uv build`、clean-wheel install 和完整 CLI smoke。
8. Phase 10 尚不可标 DONE。

## 6. 必须先修正的 draft 问题

这些问题来自当前源码核对，不是架构 blocker：

- `WorkflowHandoffManager.__init__()` 的 `trace_sink` 仍是 optional；官方 Phase 10 path 必须强制提供，不能跳过 planning freeze validation。可保留 manager 的通用构造形式，但 Phase 10 factory 必须 fail closed。
- `tests/test_phase10_workflow_contracts.py` 当前用 `"a" * 64` 作为 handoff profile hash，并非真实 `Phase10WorkflowProfileV1.profile_hash`；应构造 canonical profile fixture并更新全部 Phase 10 caller/test。
- `WorkflowHandoffManager.prepare()` 只比较传入 hash 与 trace attribute，尚未直接接收 profile object；Coordinator 必须从同一 profile object计算一次 hash并贯穿 admission/handoff/resume。
- `structural_selfcheck_dataset()` 当前 dataset/case identity 主要绑定 run ID/input hash，尚未将 `profile.profile_hash` 明确纳入 deterministic preimage；按批准语义收紧并补 fresh-instance determinism/profile-mismatch tests。
- `workflow_evaluation.py` 尚无测试；必须覆盖 exact query、dataset/case/hash、loader exact hash、wrong hash、无 dataset file、Phase 7 compatibility、零 model evaluator call和 EVALUATING replay。
- Claim Extraction filesystem suite尚未证明两个并发 writer 从同 revision 只有一个成功；缺完整 unsupported-version/unsafe-persistence matrix。
- Claim Extraction partial replay目前只覆盖“claim receipt 已存在、补 edge”；尚缺多个 edge 中仅补缺失 edge的测试。
- 多数 restart 测试复用 in-memory authorities；仍需 fresh filesystem-backed instance证明。
- DeepSeek Claim Extraction尚缺专属 mocked-transport tests：valid、malformed、schema/model identity、lifecycle、composition mismatch、reservation overflow zero calls、no retry。

## 7. 新窗口建议执行顺序

1. 完成真实 `Phase10WorkflowProfileV1` fixture builder，移除所有 dummy profile hash。
2. 收紧 profile hash 在 admission、trace、handoff、resume 的一条值链，并补 tests。
3. 修正并测试 structural self-check materialization + `InMemoryEvaluationDatasetLoader`。
4. 补齐 Claim Extraction filesystem/concurrency/partial-edge/fresh-process tests。
5. 补齐 DeepSeek Claim Extraction mocked transport/composition/admission tests。
6. 实现薄 `WorkflowCoordinator`，按 durable authorities 分支恢复，不维护 cursor。
7. 实现唯一 filesystem-backed `WorkflowFactory`，显式 MOCK/REAL profile，fresh process 可重建。
8. CLI 只做参数解析和 delegation；为 inspect 使用不会 mkdir/repair/read-secret 的只读构造路径。
9. 建 formal MOCK E2E，并 fresh factory resume，比较恢复前后所有 authority bytes/identities/counts。
10. 补 failure/recovery matrix、文档和所有 release gates。

## 8. 必须实现的 lifecycle / recovery

- `CREATED`：合法 transition 到 `PLANNING`。
- `PLANNING` 且无 `PLANNING_STARTED`：可用显式 profile materialize allocation并首次 planning。
- `PLANNING_STARTED` 已存在但无合法 handoff：planner outcome unknown，finalize `FAILED`，禁止 redispatch。
- valid `PREPARED` handoff：合法进入 `READY -> RUNNING`，创建/校验 Phase 3 checkpoint。
- `RUNNING + checkpoint`：使用现有 executor resume/execute。
- execution terminal 后：继续 Evidence、Claim Extraction、ClaimGraph。
- `RUNNING` verification entry由现有 DurableVerificationCoordinator推进到 `VERIFYING/EVALUATING`。
- `EVALUATING`：重建同一 structural self-check dataset/request并复用已有 Evaluation authority。
- terminal Run：只返回既有 durable result，不重复 side effects。

## 9. 当前验证证据

本交接生成前刚执行：

```text
uv run pytest \
  tests/test_phase10_claim_extraction_durability.py \
  tests/test_phase10_workflow_contracts.py \
  tests/test_perspective_planner.py \
  tests/test_async_dag_executor.py \
  tests/test_phase9_llm_adapters.py \
  tests/test_phase9_real_composition.py \
  -q --basetemp=.pytest_tmp -p no:cacheprovider

185 passed in 1.68s
```

同次验证：

- `uv run ruff check .`：passed。
- `git diff --check`：passed；只有 Git 的 LF/CRLF warning。
- 这不是 full suite、all-extras、build 或 clean-wheel 通过证明。

Windows 本机若默认 uv cache 权限失败，使用：

```powershell
$env:UV_CACHE_DIR='.uv-cache'
```

## 10. 当前 Git 文件边界

已跟踪修改：

```text
src/researchos/adapters/deepseek.py
src/researchos/application/async_dag_executor.py
src/researchos/application/claim_graph.py
src/researchos/application/errors.py
src/researchos/application/integration_factory.py
src/researchos/application/perspective_planner.py
src/researchos/application/provider_dispatch.py
src/researchos/application/real_composition.py
src/researchos/configuration/real_settings.py
src/researchos/configuration/validation.py
src/researchos/domain/__init__.py
tests/test_async_dag_executor.py
tests/test_perspective_planner.py
```

Phase 10 未跟踪文件：

```text
PLAN10.md
src/researchos/adapters/claim_extraction_operation_filesystem.py
src/researchos/adapters/claim_extraction_operation_memory.py
src/researchos/adapters/mock_claim_extraction.py
src/researchos/adapters/workflow_handoff_filesystem.py
src/researchos/adapters/workflow_handoff_memory.py
src/researchos/application/claim_extraction_operation.py
src/researchos/application/claim_extractor.py
src/researchos/application/execution_policy_builder.py
src/researchos/application/workflow_budget.py
src/researchos/application/workflow_evaluation.py
src/researchos/application/workflow_handoff.py
src/researchos/domain/claim_extraction.py
src/researchos/domain/claim_extraction_operation.py
src/researchos/domain/workflow.py
src/researchos/interfaces/claim_extraction.py
src/researchos/interfaces/workflow.py
tests/test_phase10_claim_extraction_durability.py
tests/test_phase10_workflow_contracts.py
```

`git diff --stat` 不包含上述 untracked files，审计时不要把普通 diff stat 当作完整 Phase 10 change set。

## 11. 给新窗口的启动指令

```text
继续 E:\ResearchOS Agent 当前 feat/phase-10-e2e-workflow 工作树。

先完整阅读 AGENTS.md、PLAN10.md、PHASE10_HANDOFF.md，以及 git status；保留全部 tracked/untracked Phase 10 工作，不 reset/clean/checkout，不 commit/push，不做 REAL 调用。

已批准：所有仅涉及未发布 Phase 10 的 API/schema/config歧义，选择最窄的 explicit、deterministic、fail-closed实现并继续，不作为 blocker。只有确实必须改变已冻结 Phase 1-9 contract 且无 wrapper/adapter 解法时才能停。

按 PHASE10_HANDOFF.md 第 6、7 节继续：先完成 canonical workflow profile fixture/hash贯穿和 structural self-check tests，然后补齐 Claim Extraction/DeepSeek tests，实施 WorkflowCoordinator、filesystem WorkflowFactory、run/resume/inspect CLI、formal MOCK E2E、recovery matrix、docs和完整 offline release validation。

禁止修改 RunConfig v1、RuntimeCheckpoint v1、ClaimGraphSnapshot v1；禁止新增第二套 runtime/budget/checkpoint/evidence/claim/verification/evaluation authority；禁止 REAL/付费调用；完成前不要发中间完成报告。
```
