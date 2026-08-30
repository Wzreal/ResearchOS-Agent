"""Canonical semantic and physical identity helpers for Phase 7."""

from __future__ import annotations

from researchos.domain.evaluation import (
    AblationResult,
    EvaluationCaseResult,
    EvaluationComparison,
    EvaluationRun,
)
from researchos.domain.identity import stable_hash


def case_semantic_hash(result: EvaluationCaseResult) -> str:
    payload = result.model_dump(
        mode="json",
        exclude={
            "started_at",
            "completed_at",
            "case_semantic_hash",
            "artifact_content_hash",
        },
    )
    return stable_hash(payload)


def case_artifact_hash(result: EvaluationCaseResult) -> str:
    return stable_hash(
        result.model_dump(mode="json", exclude={"artifact_content_hash"})
    )


def evaluation_semantic_hash(result: EvaluationRun) -> str:
    payload = result.model_dump(
        mode="json",
        exclude={
            "started_at": True,
            "completed_at": True,
            "evaluation_semantic_hash": True,
            "artifact_content_hash": True,
            "cases": {
                "__all__": {"started_at", "completed_at", "artifact_content_hash"}
            },
        },
    )
    return stable_hash(payload)


def evaluation_artifact_hash(result: EvaluationRun) -> str:
    return stable_hash(
        result.model_dump(mode="json", exclude={"artifact_content_hash"})
    )


def validate_evaluation_hashes(result: EvaluationRun) -> None:
    for case in result.cases:
        if case.case_semantic_hash != case_semantic_hash(case):
            raise ValueError("case semantic hash differs")
        if case.artifact_content_hash != case_artifact_hash(case):
            raise ValueError("case artifact content hash differs")
    if result.evaluation_semantic_hash != evaluation_semantic_hash(result):
        raise ValueError("evaluation semantic hash differs")
    if result.artifact_content_hash != evaluation_artifact_hash(result):
        raise ValueError("evaluation artifact content hash differs")


def comparison_artifact_hash(result: EvaluationComparison) -> str:
    return stable_hash(
        result.model_dump(mode="json", exclude={"artifact_content_hash"})
    )


def ablation_artifact_hash(result: AblationResult) -> str:
    return stable_hash(
        result.model_dump(mode="json", exclude={"artifact_content_hash"})
    )
