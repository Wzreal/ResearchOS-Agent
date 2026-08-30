"""Provider-independent Phase 7 boundaries."""

from __future__ import annotations

from typing import Protocol

from researchos.domain.evaluation import (
    AblationResult,
    EvaluationComparison,
    EvaluationDataset,
    EvaluationJudgeRequest,
    EvaluationJudgeResponse,
    EvaluationRun,
)
from researchos.interfaces.runtime import CancellationSignal


class EvaluationDatasetLoader(Protocol):
    def load(
        self, dataset_id: str, dataset_version: str, *, expected_hash: str | None = None
    ) -> EvaluationDataset: ...


class EvaluationArtifactStore(Protocol):
    def load(self, eval_run_id: str) -> EvaluationRun | None: ...

    def publish(self, result: EvaluationRun) -> None: ...


class EvaluationComparisonStore(Protocol):
    def load_comparison(self, comparison_id: str) -> EvaluationComparison | None: ...

    def publish_comparison(self, comparison: EvaluationComparison) -> None: ...


class EvaluationAblationStore(Protocol):
    def load_ablation(self, ablation_id: str) -> AblationResult | None: ...

    def publish_ablation(self, ablation: AblationResult) -> None: ...


class EvaluationJudge(Protocol):
    @property
    def model_bundle_hash(self) -> str: ...

    async def evaluate(
        self, request: EvaluationJudgeRequest, cancellation: CancellationSignal
    ) -> EvaluationJudgeResponse: ...
