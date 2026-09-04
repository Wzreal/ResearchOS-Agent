"""Release-gate tests for configuration and paid-smoke isolation."""

from __future__ import annotations

from pathlib import Path

import pytest
from phase9_fixtures import make_settings
from pydantic import ValidationError

from researchos.configuration.real_settings import RealModelSettings


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com:444",
        "https://deepseek.example.invalid",
        "https://user@api.deepseek.com",
        "https://api.deepseek.com?redirect=https://example.invalid",
    ),
)
def test_real_model_settings_allow_only_canonical_public_deepseek_origin(endpoint):
    values = make_settings().model_for_role("planning").model_dump(mode="python")
    values["base_endpoint"] = endpoint
    with pytest.raises(ValidationError):
        RealModelSettings.model_validate(values)


def test_real_smoke_workflow_is_manual_protected_and_ref_pinned():
    text = Path(".github/workflows/real-smoke.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request_target" not in text
    assert "push:" not in text
    assert "pull_request:" not in text
    assert "contents: read" in text
    assert "environment: researchos-real-smoke" in text
    assert "inputs.paid_call_acknowledged == true" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "refs/tags/release-" not in text
    assert "ref: ${{ github.sha }}" in text
    assert "inputs.ref" not in text
    assert "inputs.sha" not in text
