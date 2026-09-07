"""Non-persistent Phase 11 human-review and ablation declarations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from researchos.domain.contracts import ContractModel, SafeId, Sha256, _require_aware


class Phase11ReviewRubricV1(ContractModel):
    rubric_id: SafeId
    rubric_version: str = Field(min_length=1, max_length=80)
    dimensions: tuple[SafeId, ...]

    @field_validator("dimensions")
    @classmethod
    def dimensions_are_sorted_and_unique(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value or value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("review rubric dimensions must be sorted and unique")
        return value


class Phase11HumanReviewV1(ContractModel):
    run_id: SafeId
    reviewer_id: SafeId
    rubric: Phase11ReviewRubricV1
    evaluation_run_id: SafeId
    evidence_reference: str = Field(min_length=1, max_length=1_024)
    output_hash: Sha256
    scores: dict[SafeId, int]
    rationale: str = Field(min_length=1, max_length=4_000)
    disagreement_state: Literal["none", "critical", "adjudicated"] = "none"
    adjudication_reference: str | None = Field(default=None, max_length=1_024)
    reviewed_at: datetime
    blinded: bool = False
    review_kind: Literal["primary", "secondary", "adjudication"]

    _aware = field_validator("reviewed_at")(_require_aware)

    @model_validator(mode="after")
    def score_keys_match_rubric(self) -> Phase11HumanReviewV1:
        if tuple(sorted(self.scores)) != self.rubric.dimensions:
            raise ValueError("review scores must exactly match rubric dimensions")
        if any(score < 0 or score > 2 for score in self.scores.values()):
            raise ValueError("review scores must be in range 0..2")
        if self.review_kind == "secondary" and not self.blinded:
            raise ValueError("secondary review must be blinded")
        if self.disagreement_state == "adjudicated" and not self.adjudication_reference:
            raise ValueError("adjudicated review requires an adjudication reference")
        return self


class Phase11AblationSpecV1(ContractModel):
    comparison_id: Literal["max_rounds_1_vs_2", "search_browser_vs_search_only"]
    dataset_id: SafeId
    dataset_hash: Sha256
    baseline_run_ids: tuple[SafeId, ...] = ()

    @field_validator("baseline_run_ids")
    @classmethod
    def baseline_runs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("ablation baseline Run IDs must be unique")
        return value
