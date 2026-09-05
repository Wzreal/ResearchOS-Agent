"""Explicit Phase 10 materialization of an existing Phase 3 ExecutionPolicy."""

from researchos.application.errors import RuntimePreconditionError
from researchos.domain.contracts import model_sha256
from researchos.domain.planning import TaskDAG
from researchos.domain.runtime import ExecutionPolicy, RuntimeResourceAmount
from researchos.domain.workflow import Phase10ExecutionPolicyConfigV1


class ExecutionPolicyBuilder:
    """Derive task identity only; all scheduling semantics come from config."""

    def build(
        self,
        validated_dag: TaskDAG,
        execution_budget_slice: RuntimeResourceAmount,
        config: Phase10ExecutionPolicyConfigV1 | None,
    ) -> ExecutionPolicy:
        if config is None:
            raise RuntimePreconditionError(
                "Phase 10 execution policy config is required"
            )
        task_ids = {task.task_id for task in validated_dag.tasks}
        configured_ids = [item.task_id for item in config.task_policies]
        if (
            len(configured_ids) != len(set(configured_ids))
            or set(configured_ids) != task_ids
        ):
            raise RuntimePreconditionError(
                "execution policy config must cover DAG exactly"
            )
        if any(
            not item.reservation.fits_within(execution_budget_slice)
            for item in config.task_policies
        ):
            raise RuntimePreconditionError(
                "task reservation exceeds execution budget slice"
            )
        policy = ExecutionPolicy(
            max_concurrency=config.max_concurrency,
            max_replans=config.max_replans,
            tasks=tuple(sorted(config.task_policies, key=lambda item: item.task_id)),
        )
        # Hashing here forces a canonical materialized form before handoff pinning.
        model_sha256(policy)
        return policy
