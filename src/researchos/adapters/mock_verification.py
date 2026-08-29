"""Deterministic offline verification model fixtures."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from researchos.application.errors import (
    VerificationCancelled,
    VerificationModelFailure,
)
from researchos.domain.contracts import OperatingMode
from researchos.domain.identity import stable_hash
from researchos.domain.runtime import RuntimeResourceAmount, UsageCertainty
from researchos.domain.synthesis import (
    VerificationModelRequest,
    VerificationModelResponse,
    VerificationRole,
)
from researchos.interfaces.runtime import CancellationSignal


@dataclass(frozen=True, slots=True)
class VerificationFixtureKey:
    verification_id: str
    role: VerificationRole
    round_number: int
    draft_revision_id: str


@dataclass(frozen=True, slots=True)
class VerificationFixture:
    payload: dict[str, Any] | bytes
    usage: RuntimeResourceAmount = RuntimeResourceAmount()
    usage_certainty: UsageCertainty = UsageCertainty.EXACT
    response_role: VerificationRole | None = None
    response_round_number: int | None = None
    response_draft_revision_id: str | None = None


class MockVerificationModel:
    def __init__(
        self,
        fixtures: Mapping[VerificationFixtureKey, VerificationFixture],
        *,
        mode: OperatingMode = OperatingMode.MOCK,
        bundle_version: str = "mock-verification-v1",
    ) -> None:
        if mode is not OperatingMode.MOCK:
            raise VerificationModelFailure(
                "mock verification adapter requires MOCK mode"
            )
        self._fixtures = dict(fixtures)
        self.requests: list[VerificationModelRequest] = []
        self._bundle_hash = stable_hash(
            {"model_id": "mock_verification_v1", "bundle_version": bundle_version}
        )

    @property
    def model_bundle_hash(self) -> str:
        return self._bundle_hash

    async def invoke(
        self, request: VerificationModelRequest, cancellation: CancellationSignal
    ) -> VerificationModelResponse:
        if cancellation.cancelled:
            raise VerificationCancelled("verification model call cancelled")
        self.requests.append(request)
        key = VerificationFixtureKey(
            request.verification_id,
            request.role,
            request.round_number,
            request.draft_revision_id,
        )
        try:
            fixture = self._fixtures[key]
        except KeyError as exc:
            raise VerificationModelFailure(
                "no exact mock verification fixture"
            ) from exc
        raw = fixture.payload
        if isinstance(raw, dict):
            raw = json.dumps(
                raw,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        return VerificationModelResponse(
            raw_bytes=raw,
            usage=fixture.usage,
            usage_certainty=fixture.usage_certainty,
            model_id="mock_verification_v1",
            mode="mock",
            role=fixture.response_role or request.role,
            round_number=(
                request.round_number
                if fixture.response_round_number is None
                else fixture.response_round_number
            ),
            draft_revision_id=(
                fixture.response_draft_revision_id or request.draft_revision_id
            ),
        )
