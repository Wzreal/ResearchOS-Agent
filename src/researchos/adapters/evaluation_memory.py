"""In-memory Phase 7 authority store."""

from __future__ import annotations

from threading import RLock

from researchos.application.errors import (
    CorruptEvaluationArtifact,
    EvaluationArtifactConflict,
)
from researchos.application.evaluation_identity import (
    ablation_artifact_hash,
    comparison_artifact_hash,
    validate_evaluation_hashes,
)
from researchos.domain.evaluation import (
    AblationResult,
    EvaluationComparison,
    EvaluationRun,
)


class InMemoryEvaluationArtifactStore:
    def __init__(self) -> None:
        self._runs: dict[str, EvaluationRun] = {}
        self._comparisons: dict[str, EvaluationComparison] = {}
        self._ablations: dict[str, AblationResult] = {}
        self._lock = RLock()

    def load(self, eval_run_id: str) -> EvaluationRun | None:
        with self._lock:
            result = self._runs.get(eval_run_id)
        if result is None:
            return None
        try:
            validate_evaluation_hashes(result)
        except ValueError as exc:
            raise CorruptEvaluationArtifact("evaluation authority is corrupt") from exc
        return result.model_copy(deep=True)

    def publish(self, result: EvaluationRun) -> None:
        validate_evaluation_hashes(result)
        with self._lock:
            current = self._runs.get(result.eval_run_id)
            if current is not None and current != result:
                raise EvaluationArtifactConflict(
                    "same evaluation identity has another authority"
                )
            self._runs[result.eval_run_id] = result.model_copy(deep=True)

    def load_comparison(self, comparison_id: str) -> EvaluationComparison | None:
        with self._lock:
            result = self._comparisons.get(comparison_id)
        if (
            result is not None
            and result.artifact_content_hash != comparison_artifact_hash(result)
        ):
            raise CorruptEvaluationArtifact("comparison authority is corrupt")
        return None if result is None else result.model_copy(deep=True)

    def publish_comparison(self, comparison: EvaluationComparison) -> None:
        if comparison.artifact_content_hash != comparison_artifact_hash(comparison):
            raise CorruptEvaluationArtifact("comparison artifact hash differs")
        with self._lock:
            current = self._comparisons.get(comparison.comparison_id)
            if current is not None and current != comparison:
                raise EvaluationArtifactConflict("comparison identity conflict")
            self._comparisons[comparison.comparison_id] = comparison.model_copy(
                deep=True
            )

    def load_ablation(self, ablation_id: str) -> AblationResult | None:
        with self._lock:
            result = self._ablations.get(ablation_id)
        if (
            result is not None
            and result.artifact_content_hash != ablation_artifact_hash(result)
        ):
            raise CorruptEvaluationArtifact("ablation authority is corrupt")
        return None if result is None else result.model_copy(deep=True)

    def publish_ablation(self, ablation: AblationResult) -> None:
        if ablation.artifact_content_hash != ablation_artifact_hash(ablation):
            raise CorruptEvaluationArtifact("ablation artifact hash differs")
        with self._lock:
            current = self._ablations.get(ablation.ablation_id)
            if current is not None and current != ablation:
                raise EvaluationArtifactConflict("ablation identity conflict")
            self._ablations[ablation.ablation_id] = ablation.model_copy(deep=True)
