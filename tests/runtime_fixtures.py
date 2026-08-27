from __future__ import annotations

from dataclasses import dataclass

from conftest import FrozenClock, SequentialIds
from planning_fixtures import candidate_payload, planning_request

from researchos.adapters.checkpoint_memory import InMemoryCheckpointStore
from researchos.adapters.memory import InMemoryRunStore, InMemoryTraceSink
from researchos.adapters.sleeper import (
    AsyncioRunCancellationController,
    ControlledSleeper,
)
from researchos.application.async_dag_executor import AsyncDAGExecutor
from researchos.application.checkpoint_manager import CheckpointManager
from researchos.application.dag_validator import DAGValidator
from researchos.application.run_manager import RunManager
from researchos.domain.contracts import RunConfig, RunInput, RunState, RunStatus
from researchos.domain.planning import CandidatePlan, ReplanContext, TaskDAG
from researchos.domain.runtime import (
    ExecutionPolicy,
    IdempotencyMode,
    RuntimeResourceAmount,
    TaskRuntimePolicy,
)


@dataclass
class RuntimeExample:
    state: RunState
    dag: TaskDAG
    policy: ExecutionPolicy
    context: ReplanContext
    clock: FrozenClock
    trace: InMemoryTraceSink


class RuntimeIds:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, prefix: str) -> str:
        self.count += 1
        return f"rt_{prefix}_{self.count}"


def build_executor(example: RuntimeExample, backend: object):
    checkpoint_store = InMemoryCheckpointStore()
    ids = RuntimeIds()
    manager = CheckpointManager(
        store=checkpoint_store,
        trace_sink=example.trace,
        clock=example.clock,
        id_factory=ids,
    )
    cancellation = AsyncioRunCancellationController()
    sleeper = ControlledSleeper()
    executor = AsyncDAGExecutor(
        checkpoints=manager,
        backend=backend,  # type: ignore[arg-type]
        clock=example.clock,
        sleeper=sleeper,
        cancellation=cancellation,
        id_factory=ids,
    )
    return executor, manager, checkpoint_store, cancellation, sleeper


def runtime_example(
    *,
    max_concurrency: int = 2,
    max_attempts: int = 1,
    max_replans: int = 1,
    timeout_milliseconds: int = 1_000,
    idempotency: IdempotencyMode = IdempotencyMode.IDEMPOTENT,
) -> RuntimeExample:
    store = InMemoryRunStore()
    trace = InMemoryTraceSink()
    clock = FrozenClock()
    manager = RunManager(
        store=store,
        trace_sink=trace,
        clock=clock,
        id_factory=SequentialIds(),
    )
    state = manager.create(
        RunInput(query="runtime query"),
        RunConfig(allowed_capability_ids=("search", "read")),
    )
    state = manager.transition(state.run_id, RunStatus.PLANNING)
    request = planning_request(
        run_id=state.run_id,
        run_revision=state.revision,
        plan_id="plan_runtime",
    )
    candidate = candidate_payload(request)
    validator = DAGValidator()
    parsed = CandidatePlan.model_validate(candidate)
    result = validator.validate(parsed, request, "mock_model")
    assert result.valid
    dag = validator.build_dag(
        parsed,
        request,
        result,
        dag_id="dag_runtime",
        created_at=clock.now(),
    )
    state = manager.transition(state.run_id, RunStatus.READY)
    state = manager.transition(state.run_id, RunStatus.RUNNING)
    policies = tuple(
        TaskRuntimePolicy(
            task_id=task.task_id,
            operation_version="v1",
            max_attempts=max_attempts,
            backoff_milliseconds=(0,) * (max_attempts - 1),
            timeout_milliseconds=timeout_milliseconds,
            reservation=RuntimeResourceAmount(
                duration_milliseconds=timeout_milliseconds,
                tokens=max(1, task.estimate.tokens),
                cost_microunits=max(1, task.estimate.cost_microunits),
                tool_calls=task.estimate.tool_calls,
            ),
            idempotency=idempotency,
            retry_on_timeout=True,
        )
        for task in dag.tasks
    )
    return RuntimeExample(
        state=state,
        dag=dag,
        policy=ExecutionPolicy(
            max_concurrency=max_concurrency,
            tasks=policies,
            max_replans=max_replans,
        ),
        context=ReplanContext(prior_plan_id=dag.plan_id, replan_count=0),
        clock=clock,
        trace=trace,
    )
