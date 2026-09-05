"""Pure, versioned configuration for the official Phase 10 MOCK workflow."""

# ruff: noqa: E501

from __future__ import annotations

from typing import Literal

from researchos.domain.claim_extraction import ClaimExtractionPolicy
from researchos.domain.contracts import ContractModel, Sha256, model_sha256
from researchos.domain.evaluation import EvaluationPolicy
from researchos.domain.planning import PlanningPolicy
from researchos.domain.runtime import (
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskRuntimePolicy,
)
from researchos.domain.synthesis import VerificationPolicy
from researchos.domain.workflow import (
    Phase10EvaluationProfileV1,
    Phase10ExecutionPolicyConfigV1,
    Phase10WorkflowBudgetConfigV1,
    Phase10WorkflowProfileV1,
    WorkflowBudgetAllocation,
)


class Phase10MockWorkflowBundleV1(ContractModel):
    bundle_id: Literal["phase10_mock"] = "phase10_mock"
    bundle_version: Literal["1"] = "1"
    planning_adapter_id: str = "mock_planning_v1"
    runtime_adapter_id: str = "scripted_agent_v1"
    claim_extraction_adapter_id: str = "mock_claim_extraction_v1"
    verification_adapter_id: str = "mock_verification_v1"
    workflow_profile: Phase10WorkflowProfileV1

    @property
    def bundle_hash(self) -> Sha256:
        return model_sha256(self)


def build_phase10_mock_bundle_v1() -> Phase10MockWorkflowBundleV1:
    total = RuntimeResourceAmount(
        duration_milliseconds=10_000, tokens=1_000, cost_microunits=1_000, tool_calls=10
    )
    allocation = WorkflowBudgetAllocation(
        total=total,
        planning=RuntimeResourceAmount(duration_milliseconds=1_000, tokens=100),
        execution=RuntimeResourceAmount(
            duration_milliseconds=6_000, tokens=600, cost_microunits=600, tool_calls=6
        ),
        claim_extraction=RuntimeResourceAmount(
            duration_milliseconds=1_000, tokens=100, cost_microunits=100, tool_calls=1
        ),
        verification=RuntimeResourceAmount(
            duration_milliseconds=2_000, tokens=200, cost_microunits=300, tool_calls=3
        ),
    )
    tasks = tuple(
        TaskRuntimePolicy(
            task_id=task_id,
            operation_version="v1",
            timeout_milliseconds=5_000,
            reservation=RuntimeResourceAmount(
                duration_milliseconds=5_000,
                tokens=100,
                cost_microunits=100,
                tool_calls=1,
            ),
            idempotency=IdempotencyMode.IDEMPOTENT,
        )
        for task_id in ("task_a", "task_b", "task_c")
    )
    return Phase10MockWorkflowBundleV1(
        workflow_profile=Phase10WorkflowProfileV1(
            profile_id="phase10_mock_profile",
            profile_version="1",
            system_commit_sha="0000000000000000000000000000000000000001",
            system_version="phase10-mock-v1",
            budget=Phase10WorkflowBudgetConfigV1(
                profile_id="phase10_mock_budget",
                profile_version="1",
                allocation=allocation,
            ),
            planning_policy=PlanningPolicy(
                max_tasks=10, max_graph_depth=5, max_dependencies_per_task=3
            ),
            execution=Phase10ExecutionPolicyConfigV1(
                max_concurrency=2, max_replans=0, task_policies=tasks
            ),
            claim_extraction_policy=ClaimExtractionPolicy(
                max_evidence_items=10,
                max_context_bytes=100_000,
                max_claims=10,
                max_references_per_claim=5,
                max_statement_bytes=10_000,
                max_response_bytes=100_000,
            ),
            verification_policy=VerificationPolicy(max_rounds=1),
            evaluation=Phase10EvaluationProfileV1(
                evaluation_policy=EvaluationPolicy(
                    policy_id="phase10_selfcheck",
                    policy_version="1",
                    enabled_evaluator_ids=("deterministic_core",),
                    allow_nonterminal_runs=True,
                )
            ),
        )
    )
