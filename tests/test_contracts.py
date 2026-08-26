from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from researchos.domain.contracts import (
    ArtifactRecord,
    Budget,
    BudgetLimits,
    BudgetUsage,
    RunConfig,
    TaskRecord,
)


def test_budget_defaults_are_effective_limits() -> None:
    config = RunConfig()

    assert config.budget_limits == BudgetLimits(
        max_duration_seconds=3_600,
        max_tokens=100_000,
        max_cost_microunits=10_000_000,
        cost_currency="USD",
        max_tool_calls=100,
    )
    assert all(
        value is not None
        for value in config.budget_limits.model_dump(mode="python").values()
    )


def test_budget_rejects_usage_over_limits() -> None:
    with pytest.raises(ValidationError, match="tokens exceed budget"):
        Budget(
            limits=BudgetLimits(max_tokens=10),
            usage=BudgetUsage(tokens=11),
        )


def test_pydantic_only_requires_aware_deadline() -> None:
    future_in_the_model_past_in_reality = datetime(2000, 1, 1, tzinfo=UTC)
    assert RunConfig(deadline=future_in_the_model_past_in_reality).deadline is not None

    with pytest.raises(ValidationError, match="timezone-aware"):
        RunConfig(deadline=datetime(2030, 1, 1))


def test_task_record_forbids_phase_2_fields() -> None:
    now = datetime(2026, 8, 26, tzinfo=UTC)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskRecord(
            task_id="task_1",
            created_at=now,
            updated_at=now,
            dependencies=[],
        )


@pytest.mark.parametrize(
    "path",
    ["/absolute.txt", "C:/secret.txt", "../escape.txt", "a/../b", "a\\b"],
)
def test_artifact_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="artifact path"):
        ArtifactRecord(
            artifact_id="artifact_1",
            kind="report",
            relative_path=path,
            media_type="text/plain",
            sha256="0" * 64,
            size_bytes=1,
            created_at=datetime(2026, 8, 26, tzinfo=UTC),
        )


def test_artifact_accepts_safe_relative_path() -> None:
    artifact = ArtifactRecord(
        artifact_id="artifact_1",
        kind="report",
        relative_path="artifacts/report.txt",
        media_type="text/plain",
        sha256="0" * 64,
        size_bytes=1,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )
    assert artifact.relative_path == "artifacts/report.txt"
