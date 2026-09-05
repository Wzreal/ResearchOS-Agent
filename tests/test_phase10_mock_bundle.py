from __future__ import annotations

from researchos.configuration.phase10_mock import build_phase10_mock_bundle_v1


def test_phase10_mock_bundle_is_canonical_and_freshly_reconstructible() -> None:
    first = build_phase10_mock_bundle_v1()
    second = build_phase10_mock_bundle_v1()

    assert first == second
    assert first.bundle_hash == second.bundle_hash
    assert first.workflow_profile.profile_hash == second.workflow_profile.profile_hash
    assert first.bundle_id == "phase10_mock"
    assert first.bundle_version == "1"
    assert first.workflow_profile.system_version == "phase10-mock-v1"
    assert not any(
        hasattr(first, attribute)
        for attribute in (
            "planning_model",
            "agent",
            "tools",
            "claim_extraction_model",
            "verification_model",
        )
    )
