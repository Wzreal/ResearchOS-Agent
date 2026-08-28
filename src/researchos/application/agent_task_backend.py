"""Phase 3 TaskExecutionBackend implemented through a bounded AgentRunner."""

from researchos.application.agent_runner import AgentRunner
from researchos.domain.agent import AgentContext, AgentExecutionStatus
from researchos.domain.runtime import (
    ExecutionResultStatus,
    TaskExecutionRequest,
    TaskExecutionResult,
)
from researchos.interfaces.runtime import CancellationSignal


class AgentTaskExecutionBackend:
    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    async def execute(
        self, request: TaskExecutionRequest, cancellation: CancellationSignal
    ) -> TaskExecutionResult:
        context = AgentContext(
            run_id=request.run_id,
            run_revision=request.run_revision,
            dag_id=request.dag_id,
            task=request.task,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            attempt_number=request.attempt_number,
            task_operation_key=request.operation_key,
            task_attempt_key=request.attempt_key,
            task_idempotency=request.task_idempotency,
            deadline=request.deadline,
            hard_limits=request.hard_limits,
            prior_backend_receipt=request.prior_backend_receipt,
            expected_outputs=request.task.expected_outputs,
            authorized_capability_ids=request.task.required_capability_ids,
        )
        result = await self._runner.run(context, cancellation)
        if result.status is AgentExecutionStatus.FAILED:
            assert result.error is not None
            return TaskExecutionResult(
                status=ExecutionResultStatus.FAILED,
                failure_code=result.error.code,
                retryable=result.error.retryable,
                usage=result.usage,
                usage_certainty=result.usage_certainty,
                backend_receipt=result.backend_receipt,
            )
        assert result.final is not None
        produced = tuple(sorted(item.output_id for item in result.final.outputs))
        expected = tuple(
            sorted(item.output_id for item in request.task.expected_outputs)
        )
        if produced != expected:
            return TaskExecutionResult(
                status=ExecutionResultStatus.FAILED,
                failure_code="agent_output_contract_violation",
                retryable=False,
                usage=result.usage,
                usage_certainty=result.usage_certainty,
                backend_receipt=result.backend_receipt,
            )
        return TaskExecutionResult(
            status=ExecutionResultStatus.SUCCEEDED,
            usage=result.usage,
            usage_certainty=result.usage_certainty,
            produced_output_ids=produced,
            backend_receipt=result.backend_receipt,
        )
