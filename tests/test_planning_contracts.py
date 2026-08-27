from __future__ import annotations

import pytest
from pydantic import ValidationError

from researchos.application.dag_validator import DAGValidator
from researchos.domain.planning import (
    PlanningError,
    PlanningResult,
    PlanningStatus,
    ReplanContext,
    ValidationIssue,
    ValidationIssueCode,
    ValidationResult,
)


def base_result(**overrides):
    values = {
        "status": PlanningStatus.MODEL_ERROR,
        "run_id": "run_test",
        "plan_id": "plan_test",
        "planning_request_id": "preq_test",
        "validation": None,
        "planning_error": PlanningError(code="model_error", message="failed"),
        "replan_context": ReplanContext(
            prior_plan_id="plan_test", replan_count=0
        ),
    }
    values.update(overrides)
    return PlanningResult(**values)


def test_model_error_has_no_fabricated_validation() -> None:
    result = base_result()
    assert result.validation is None
    assert result.planning_error is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"validation": ValidationResult(valid=True, issues=())},
        {"planning_error": None},
        {"plan_id": "plan_other"},
    ],
)
def test_model_error_invariants_reject_invalid_combinations(overrides) -> None:
    with pytest.raises(ValidationError):
        base_result(**overrides)


def test_malformed_requires_candidate_malformed_issue() -> None:
    other_issue = ValidationIssue(
        code=ValidationIssueCode.EMPTY_OBJECTIVE,
        message="empty",
    )
    with pytest.raises(ValidationError):
        base_result(
            status=PlanningStatus.MALFORMED,
            validation=ValidationResult(valid=False, issues=(other_issue,)),
            planning_error=None,
        )
    malformed = DAGValidator.malformed_result("bad payload")
    result = base_result(
        status=PlanningStatus.MALFORMED,
        validation=malformed,
        planning_error=None,
    )
    assert result.validation is malformed
