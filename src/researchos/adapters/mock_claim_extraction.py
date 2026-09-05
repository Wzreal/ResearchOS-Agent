"""Exact-fixture offline ClaimExtractionModel."""

from dataclasses import dataclass

from researchos.application.errors import PlanningModelFailure
from researchos.domain.claim_extraction import (
    ClaimExtractionModelResult,
    ClaimExtractionRequest,
    ClaimExtractionResponse,
)
from researchos.domain.identity import stable_hash
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty


@dataclass(frozen=True, slots=True)
class ClaimExtractionFixtureKey:
    evidence_snapshot_hash: str


class MockClaimExtractionModel:
    model_bundle_hash = stable_hash("mock-claim-extraction-v1")

    def __init__(self, fixtures: dict[ClaimExtractionFixtureKey, object]) -> None:
        self._fixtures = fixtures
        self.invocation_count = 0

    def generate(self, request: ClaimExtractionRequest) -> ClaimExtractionModelResult:
        self.invocation_count += 1
        try:
            fixture = self._fixtures[
                ClaimExtractionFixtureKey(request.evidence_snapshot_hash)
            ]
        except KeyError as exc:
            raise PlanningModelFailure(
                "claim_extraction_fixture_missing", "fixture missing"
            ) from exc
        if isinstance(fixture, Exception):
            raise fixture
        return ClaimExtractionModelResult(
            response=ClaimExtractionResponse.model_validate(fixture),
            usage=RuntimeResourceAmount(),
            usage_certainty=UsageCertainty.EXACT,
        )
