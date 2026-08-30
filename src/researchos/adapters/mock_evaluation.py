"""Exact-fixture MOCK-only Phase 7 evaluation judge."""

from __future__ import annotations

from researchos.application.errors import EvaluationModelFailure
from researchos.domain.evaluation import EvaluationJudgeRequest, EvaluationJudgeResponse
from researchos.interfaces.runtime import CancellationSignal


class MockEvaluationJudge:
    def __init__(
        self,
        *,
        model_bundle_hash: str,
        fixtures: dict[str, EvaluationJudgeResponse],
        mode: str = "mock",
    ) -> None:
        if mode != "mock":
            raise EvaluationModelFailure("Phase 7 has no REAL evaluation judge")
        self._model_bundle_hash = model_bundle_hash
        self._fixtures = dict(fixtures)
        self.calls = 0
        self.requests: list[EvaluationJudgeRequest] = []

    @property
    def model_bundle_hash(self) -> str:
        return self._model_bundle_hash

    async def evaluate(
        self, request: EvaluationJudgeRequest, cancellation: CancellationSignal
    ) -> EvaluationJudgeResponse:
        if cancellation.cancelled:
            raise EvaluationModelFailure("evaluation judge was cancelled")
        self.calls += 1
        self.requests.append(request.model_copy(deep=True))
        try:
            result = self._fixtures[request.fixture_key]
        except KeyError as exc:
            raise EvaluationModelFailure("evaluation judge fixture is missing") from exc
        if (
            result.fixture_key != request.fixture_key
            or result.evaluator_id != request.evaluator_id
            or result.evaluator_version != request.evaluator_version
            or result.model_bundle_hash != self._model_bundle_hash
        ):
            raise EvaluationModelFailure("evaluation judge fixture identity differs")
        return result.model_copy(deep=True)
