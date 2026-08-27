"""Deterministic structural and policy validation for Phase 2 task DAGs."""

from __future__ import annotations

import heapq
import json
from collections import Counter
from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from researchos.domain.contracts import SafeId
from researchos.domain.planning import (
    CandidatePlan,
    ExpectedOutput,
    Perspective,
    PlanningRequest,
    ResearchTask,
    ResourceEstimate,
    TaskDAG,
    TaskDependency,
    ValidationIssue,
    ValidationIssueCode,
    ValidationResult,
    ValidationSeverity,
    ValidationSummary,
)

_SAFE_ID = TypeAdapter(SafeId)


class DAGValidator:
    """Validate candidates without execution, scheduling, or side effects."""

    def validate(
        self,
        candidate: CandidatePlan,
        request: PlanningRequest,
        response_planning_model_id: str,
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []

        def add(
            code: ValidationIssueCode,
            message: str,
            *,
            task_id: str | None = None,
            perspective_id: str | None = None,
            details: dict[str, object] | None = None,
        ) -> None:
            issues.append(
                ValidationIssue(
                    code=code,
                    message=message,
                    task_id=task_id,
                    perspective_id=perspective_id,
                    details=details or {},
                )
            )

        if candidate.run_id != request.run_id:
            add(
                ValidationIssueCode.REQUEST_RUN_MISMATCH,
                "candidate run_id does not match planning request",
                details={"candidate": candidate.run_id, "expected": request.run_id},
            )
        if candidate.plan_id != request.plan_id:
            add(
                ValidationIssueCode.REQUEST_PLAN_MISMATCH,
                "candidate plan_id does not match planning request",
                details={"candidate": candidate.plan_id, "expected": request.plan_id},
            )
        metadata_model_id = candidate.planner_metadata.planning_model_id
        if metadata_model_id != response_planning_model_id:
            add(
                ValidationIssueCode.PLANNER_PROVENANCE_MISMATCH,
                "response and candidate planning model IDs do not match",
                details={
                    "candidate": metadata_model_id,
                    "response": response_planning_model_id,
                },
            )

        perspective_ids = [item.perspective_id for item in candidate.perspectives]
        perspective_counts = Counter(perspective_ids)
        for perspective in candidate.perspectives:
            perspective_id = perspective.perspective_id
            if perspective_counts[perspective_id] > 1:
                add(
                    ValidationIssueCode.DUPLICATE_PERSPECTIVE_ID,
                    "perspective ID is duplicated",
                    perspective_id=perspective_id,
                )
            if (
                not self._is_safe_id(perspective_id)
                or not perspective.name.strip()
                or not perspective.title.strip()
                or not perspective.goal.strip()
                or not perspective.rationale.strip()
                or not 1 <= perspective.priority <= 100
            ):
                add(
                    ValidationIssueCode.INVALID_PERSPECTIVE,
                    "perspective identity and descriptive fields must be valid",
                    perspective_id=perspective_id,
                )

        task_ids = [item.task_id for item in candidate.tasks]
        task_counts = Counter(task_ids)
        known_tasks = set(task_ids)
        known_perspectives = set(perspective_ids)
        graph: dict[str, set[str]] = {task_id: set() for task_id in known_tasks}
        reverse_graph: dict[str, set[str]] = {task_id: set() for task_id in known_tasks}

        if len(candidate.tasks) > request.policy.max_tasks:
            add(
                ValidationIssueCode.TASK_LIMIT_EXCEEDED,
                "candidate exceeds maximum task count",
                details={
                    "actual": len(candidate.tasks),
                    "limit": request.policy.max_tasks,
                },
            )

        perspective_usage: Counter[str] = Counter()
        for task in candidate.tasks:
            task_id = task.task_id
            perspective_usage[task.perspective_id] += 1
            if task_counts[task_id] > 1:
                add(
                    ValidationIssueCode.DUPLICATE_TASK_ID,
                    "task ID is duplicated",
                    task_id=task_id,
                )
            if not self._is_safe_id(task_id):
                add(
                    ValidationIssueCode.INVALID_TASK_ID,
                    "task ID is not a safe stable identifier",
                    task_id=task_id,
                )
            if task.perspective_id not in known_perspectives:
                add(
                    ValidationIssueCode.UNKNOWN_PERSPECTIVE,
                    "task references an unknown perspective",
                    task_id=task_id,
                    perspective_id=task.perspective_id,
                )
            if not task.objective.strip():
                add(
                    ValidationIssueCode.EMPTY_OBJECTIVE,
                    "task objective must not be blank",
                    task_id=task_id,
                )

            dependency_ids = [dependency.task_id for dependency in task.dependencies]
            dependency_counts = Counter(dependency_ids)
            if len(dependency_ids) > request.policy.max_dependencies_per_task:
                add(
                    ValidationIssueCode.DEPENDENCY_LIMIT_EXCEEDED,
                    "task exceeds maximum dependency count",
                    task_id=task_id,
                    details={
                        "actual": len(dependency_ids),
                        "limit": request.policy.max_dependencies_per_task,
                    },
                )
            for dependency_id in dependency_ids:
                if dependency_counts[dependency_id] > 1:
                    add(
                        ValidationIssueCode.DUPLICATE_DEPENDENCY,
                        "dependency is duplicated within task",
                        task_id=task_id,
                        details={"dependency_task_id": dependency_id},
                    )
                if dependency_id == task_id:
                    add(
                        ValidationIssueCode.SELF_DEPENDENCY,
                        "task cannot depend on itself",
                        task_id=task_id,
                    )
                elif dependency_id not in known_tasks:
                    add(
                        ValidationIssueCode.UNKNOWN_DEPENDENCY,
                        "task references an unknown dependency",
                        task_id=task_id,
                        details={"dependency_task_id": dependency_id},
                    )
                elif task_counts[task_id] == 1 and task_counts[dependency_id] == 1:
                    graph[dependency_id].add(task_id)
                    reverse_graph[task_id].add(dependency_id)

            capability_counts = Counter(task.required_capability_ids)
            for capability_id in task.required_capability_ids:
                if capability_counts[capability_id] > 1:
                    add(
                        ValidationIssueCode.DUPLICATE_CAPABILITY,
                        "required capability is duplicated",
                        task_id=task_id,
                        details={"capability_id": capability_id},
                    )
                if not self._is_safe_id(capability_id):
                    add(
                        ValidationIssueCode.INVALID_CAPABILITY_ID,
                        "required capability ID is invalid",
                        task_id=task_id,
                        details={"capability_id": capability_id},
                    )
                elif capability_id not in request.allowed_capability_ids:
                    add(
                        ValidationIssueCode.UNAUTHORIZED_CAPABILITY,
                        "required capability is not allowed by the run configuration",
                        task_id=task_id,
                        details={"capability_id": capability_id},
                    )

            output_ids = [output.output_id for output in task.expected_outputs]
            if not output_ids or any(
                not self._is_safe_id(output.output_id)
                or not output.description.strip()
                or not output.media_type.strip()
                for output in task.expected_outputs
            ):
                add(
                    ValidationIssueCode.INVALID_EXPECTED_OUTPUT,
                    "task must declare identified non-blank expected outputs",
                    task_id=task_id,
                )
            for output_id, count in Counter(output_ids).items():
                if count > 1:
                    add(
                        ValidationIssueCode.DUPLICATE_EXPECTED_OUTPUT,
                        "expected output ID is duplicated within task",
                        task_id=task_id,
                        details={"output_id": output_id},
                    )

        for perspective_id in sorted(known_perspectives):
            if (
                perspective_counts[perspective_id] == 1
                and not perspective_usage[perspective_id]
            ):
                add(
                    ValidationIssueCode.ORPHAN_PERSPECTIVE,
                    "perspective has no research task",
                    perspective_id=perspective_id,
                )

        unsafe_codes = {
            ValidationIssueCode.DUPLICATE_TASK_ID,
            ValidationIssueCode.INVALID_TASK_ID,
            ValidationIssueCode.UNKNOWN_DEPENDENCY,
            ValidationIssueCode.SELF_DEPENDENCY,
            ValidationIssueCode.DUPLICATE_DEPENDENCY,
        }
        structurally_safe = not any(issue.code in unsafe_codes for issue in issues)
        topo: list[str] = []
        graph_depth: int | None = None
        critical_duration: int | None = None
        if structurally_safe:
            topo, graph_depth, critical_duration, cycle = self._graph_metrics(
                candidate, graph, reverse_graph
            )
            if cycle:
                add(
                    ValidationIssueCode.CYCLE_DETECTED,
                    "task dependencies contain a cycle",
                    details={"cycle": cycle},
                )
                topo = []
                graph_depth = None
                critical_duration = None
                structurally_safe = False
        if (
            structurally_safe
            and graph_depth is not None
            and graph_depth > request.policy.max_graph_depth
        ):
            add(
                ValidationIssueCode.DEPTH_LIMIT_EXCEEDED,
                "task graph exceeds maximum depth",
                details={
                    "actual": graph_depth,
                    "limit": request.policy.max_graph_depth,
                },
            )

        total = ResourceEstimate()
        for task in candidate.tasks:
            total = total.plus(task.estimate)
        remaining = request.remaining_budget
        if (
            critical_duration is not None
            and critical_duration > remaining.duration_milliseconds
        ):
            add(
                ValidationIssueCode.BUDGET_DURATION_EXCEEDED,
                "estimated critical path duration exceeds remaining budget",
                details={
                    "actual": critical_duration,
                    "remaining": remaining.duration_milliseconds,
                },
            )
        for actual, limit, code, name in (
            (
                total.tokens,
                remaining.tokens,
                ValidationIssueCode.BUDGET_TOKENS_EXCEEDED,
                "tokens",
            ),
            (
                total.cost_microunits,
                remaining.cost_microunits,
                ValidationIssueCode.BUDGET_COST_EXCEEDED,
                "cost",
            ),
            (
                total.tool_calls,
                remaining.tool_calls,
                ValidationIssueCode.BUDGET_TOOL_CALLS_EXCEEDED,
                "tool calls",
            ),
        ):
            if actual > limit:
                add(
                    code,
                    f"estimated {name} exceeds remaining budget",
                    details={"actual": actual, "remaining": limit},
                )

        sorted_issues = tuple(sorted(issues, key=self._issue_key))
        valid = not any(
            issue.severity is ValidationSeverity.ERROR for issue in sorted_issues
        )
        return ValidationResult(
            valid=valid,
            issues=sorted_issues,
            topological_order=tuple(topo) if structurally_safe else (),
            graph_depth=graph_depth,
            total_estimate=total,
            critical_path_duration_milliseconds=critical_duration,
            perspective_count=len(candidate.perspectives),
            task_count=len(candidate.tasks),
        )

    def build_dag(
        self,
        candidate: CandidatePlan,
        request: PlanningRequest,
        validation: ValidationResult,
        *,
        dag_id: str,
        created_at: datetime,
    ) -> TaskDAG:
        """Build the strict canonical contract after successful validation."""

        if not validation.valid:
            raise ValueError("cannot build a DAG from invalid validation")
        if (
            validation.graph_depth is None
            or validation.critical_path_duration_milliseconds is None
        ):
            raise ValueError("cannot build a DAG without reliable graph metrics")
        perspectives = tuple(
            Perspective(
                perspective_id=item.perspective_id,
                name=item.name.strip(),
                title=item.title.strip(),
                goal=item.goal.strip(),
                rationale=item.rationale.strip(),
                priority=item.priority,
            )
            for item in sorted(
                candidate.perspectives, key=lambda item: item.perspective_id
            )
        )
        tasks = tuple(
            ResearchTask(
                task_id=item.task_id,
                perspective_id=item.perspective_id,
                objective=item.objective.strip(),
                dependencies=tuple(
                    TaskDependency(task_id=dependency.task_id)
                    for dependency in sorted(
                        item.dependencies, key=lambda dependency: dependency.task_id
                    )
                ),
                required_capability_ids=tuple(sorted(item.required_capability_ids)),
                expected_outputs=tuple(
                    ExpectedOutput(
                        output_id=output.output_id,
                        description=output.description.strip(),
                        media_type=output.media_type.strip(),
                    )
                    for output in sorted(
                        item.expected_outputs,
                        key=lambda output: output.output_id,
                    )
                ),
                estimate=item.estimate,
                policy=item.policy,
            )
            for item in sorted(candidate.tasks, key=lambda item: item.task_id)
        )
        summary = ValidationSummary(
            graph_depth=validation.graph_depth,
            total_estimate=validation.total_estimate,
            critical_path_duration_milliseconds=(
                validation.critical_path_duration_milliseconds
            ),
            perspective_count=validation.perspective_count,
            task_count=validation.task_count,
        )
        return TaskDAG(
            dag_id=dag_id,
            plan_id=candidate.plan_id,
            run_id=candidate.run_id,
            run_revision=request.run_revision,
            planning_request_id=request.request_id,
            perspectives=perspectives,
            tasks=tasks,
            topological_order=validation.topological_order,
            planner_metadata=candidate.planner_metadata,
            validation_summary=summary,
            created_at=created_at,
        )

    @staticmethod
    def malformed_result(errors: list[dict[str, object]]) -> ValidationResult:
        issue = ValidationIssue(
            code=ValidationIssueCode.CANDIDATE_MALFORMED,
            message="candidate plan payload is malformed",
            details={"errors": errors},
        )
        return ValidationResult(valid=False, issues=(issue,))

    @staticmethod
    def _is_safe_id(value: str) -> bool:
        try:
            _SAFE_ID.validate_python(value)
        except ValidationError:
            return False
        return True

    @staticmethod
    def _issue_key(issue: ValidationIssue) -> tuple[str, str, str, str, str]:
        return (
            issue.code.value,
            issue.perspective_id or "",
            issue.task_id or "",
            issue.message,
            json.dumps(issue.details, sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _graph_metrics(
        candidate: CandidatePlan,
        graph: dict[str, set[str]],
        reverse_graph: dict[str, set[str]],
    ) -> tuple[list[str], int, int, list[str]]:
        indegree = {task_id: len(reverse_graph[task_id]) for task_id in graph}
        ready = [task_id for task_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        topo: list[str] = []
        depth = {task_id: 1 for task_id in ready}
        durations = {
            task.task_id: task.estimate.duration_milliseconds
            for task in candidate.tasks
            if task.task_id in graph
        }
        critical = {task_id: durations.get(task_id, 0) for task_id in ready}
        while ready:
            task_id = heapq.heappop(ready)
            topo.append(task_id)
            for child in sorted(graph[task_id]):
                depth[child] = max(depth.get(child, 1), depth[task_id] + 1)
                critical[child] = max(
                    critical.get(child, durations.get(child, 0)),
                    critical[task_id] + durations.get(child, 0),
                )
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, child)
        if len(topo) == len(graph):
            return (
                topo,
                max(depth.values(), default=0),
                max(critical.values(), default=0),
                [],
            )

        remaining = sorted(set(graph) - set(topo))
        cycle = DAGValidator._cycle_witness(graph, remaining)
        return (
            [],
            max(depth.values(), default=0),
            max(critical.values(), default=0),
            cycle,
        )

    @staticmethod
    def _cycle_witness(graph: dict[str, set[str]], nodes: list[str]) -> list[str]:
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            state[node] = 1
            stack.append(node)
            for child in sorted(graph[node]):
                if child not in nodes:
                    continue
                if state.get(child, 0) == 0:
                    found = visit(child)
                    if found:
                        return found
                elif state[child] == 1:
                    start = stack.index(child)
                    return [*stack[start:], child]
            stack.pop()
            state[node] = 2
            return None

        for node in nodes:
            if state.get(node, 0) == 0:
                found = visit(node)
                if found:
                    return found
        return nodes
