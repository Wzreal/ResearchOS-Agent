"""Deterministic offline PlanningModel adapter for Phase 2 tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from researchos.application.errors import PlanningModelFailure
from researchos.domain.contracts import RunInput
from researchos.domain.planning import PlanningModelResponse, PlanningRequest
from researchos.security.redaction import PersistenceRedactor


@dataclass(frozen=True, slots=True)
class PlanningFixtureKey:
    normalized_query: str
    replan_count: int
    reason_code: str | None


PlanningFixture = (
    PlanningModelResponse
    | PlanningModelFailure
    | Callable[[PlanningRequest], PlanningModelResponse]
)


class MockPlanningModel:
    """Select an exact fixture; never synthesize success or silently fall back."""

    def __init__(
        self,
        fixtures: Mapping[PlanningFixtureKey, PlanningFixture],
        *,
        redactor: PersistenceRedactor | None = None,
    ) -> None:
        self._fixtures = dict(fixtures)
        self._redactor = redactor or PersistenceRedactor()
        self.requests: list[PlanningRequest] = []

    def generate(self, request: PlanningRequest) -> PlanningModelResponse:
        normalized = self._redactor.normalize_input(RunInput(query=request.query)).query
        key = PlanningFixtureKey(
            normalized_query=normalized,
            replan_count=request.replan_count,
            reason_code=request.reason_code,
        )
        self.requests.append(request)
        try:
            fixture = self._fixtures[key]
        except KeyError as exc:
            raise PlanningModelFailure(
                "mock_fixture_not_found",
                "no exact deterministic planning fixture matched the request",
            ) from exc
        if isinstance(fixture, PlanningModelFailure):
            raise PlanningModelFailure(
                fixture.code, str(fixture), retryable=fixture.retryable
            )
        response = fixture(request) if callable(fixture) else fixture
        return response.model_copy(deep=True)
