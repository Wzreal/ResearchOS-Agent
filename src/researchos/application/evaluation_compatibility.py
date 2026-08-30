"""Case/run compatibility and homogeneous SUT provenance."""

from __future__ import annotations

from researchos.application.errors import (
    CaseRunCompatibilityError,
    SUTHomogeneityError,
)
from researchos.application.evaluation_artifacts import FrozenRunArtifacts
from researchos.domain.contracts import RunInput, model_sha256
from researchos.domain.evaluation import (
    ArtifactAvailability,
    ArtifactKind,
    BudgetConditionKind,
    CaseRunCompatibility,
    CompatibilityIssue,
    EvaluationCase,
    ProvenanceGrade,
    SourcePolicyRequirementKind,
    SUTPinObservation,
    SUTPinStatus,
    SUTPinSupportKind,
    SUTPinSupportRef,
    SystemUnderTest,
)
from researchos.domain.identity import stable_hash
from researchos.security.redaction import prepare_persistent_run_input


def validate_case_run(
    case: EvaluationCase, frozen: FrozenRunArtifacts
) -> CaseRunCompatibility:
    persistent_input, _ = prepare_persistent_run_input(RunInput(query=case.query))
    case_hash = model_sha256(persistent_input)
    state = frozen.run_state
    if state is None:
        raise CaseRunCompatibilityError("run state artifact is corrupt")
    conditions = case.required_execution_conditions
    issues: list[CompatibilityIssue] = []

    def issue(code: str, field: str) -> None:
        issues.append(CompatibilityIssue(code=code, field=field))

    if case_hash != state.input_hash:
        issue("input_hash_mismatch", "query")
    source = conditions.source_policy
    if (
        source.kind is SourcePolicyRequirementKind.REQUIRE_NONE
        and state.config.source_policy_id is not None
    ):
        issue("source_policy_must_be_none", "source_policy")
    elif (
        source.kind is SourcePolicyRequirementKind.REQUIRE_EXACT
        and state.config.source_policy_id != source.policy_id
    ):
        issue("source_policy_mismatch", "source_policy")
    if (
        conditions.operating_mode is not None
        and state.config.mode is not conditions.operating_mode
    ):
        issue("operating_mode_mismatch", "operating_mode")
    if (
        conditions.output_format is not None
        and state.config.output_format != conditions.output_format
    ):
        issue("output_format_mismatch", "output_format")
    required = conditions.required_allowed_capability_ids
    if required is not None and not set(required).issubset(
        state.config.allowed_capability_ids
    ):
        issue("allowed_capabilities_missing", "allowed_capabilities")
    if conditions.budget is not None:
        actual = state.config.budget_limits
        wanted = conditions.budget.limits
        if conditions.budget.kind is BudgetConditionKind.EXACT:
            compatible = actual == wanted
        else:
            compatible = (
                actual.cost_currency == wanted.cost_currency
                and actual.max_duration_seconds >= wanted.max_duration_seconds
                and actual.max_tokens >= wanted.max_tokens
                and actual.max_cost_microunits >= wanted.max_cost_microunits
                and actual.max_tool_calls >= wanted.max_tool_calls
            )
        if not compatible:
            issue("budget_condition_mismatch", "budget")
    result = CaseRunCompatibility(
        case_id=case.case_id,
        run_id=state.run_id,
        case_input_hash=case_hash,
        run_input_hash=state.input_hash,
        compatible=not issues,
        issues=tuple(sorted(issues, key=lambda item: (item.field, item.code))),
    )
    if not result.compatible:
        raise CaseRunCompatibilityError(
            f"case {case.case_id} is incompatible with run {state.run_id}"
        )
    return result


def observe_sut(frozen: FrozenRunArtifacts) -> tuple[SUTPinObservation, ...]:
    manifest_hash = frozen.manifest.manifest_hash

    def artifact_pin(
        pin_id: str, value: object, artifact: ArtifactKind
    ) -> SUTPinObservation:
        ref = next(
            item for item in frozen.manifest.artifacts if item.artifact_kind is artifact
        )
        return SUTPinObservation(
            pin_id=pin_id,
            pin_version="1",
            value_hash=stable_hash(value),
            status=SUTPinStatus.ARTIFACT_VERIFIED,
            supporting_ref=SUTPinSupportRef(
                kind=SUTPinSupportKind.ARTIFACT,
                input_manifest_hash=manifest_hash,
                artifact_kind=artifact,
                artifact_sha256=ref.sha256,
            ),
        )

    pins = [
        artifact_pin(
            "run_schema", frozen.run_state.schema_version, ArtifactKind.RUN_STATE
        ),
        artifact_pin(
            "operating_mode", frozen.run_state.config.mode.value, ArtifactKind.RUN_STATE
        ),
        artifact_pin(
            "run_config", frozen.run_state.config_hash, ArtifactKind.RUN_STATE
        ),
    ]
    if frozen.checkpoint is not None:
        metadata = frozen.checkpoint.dag.planner_metadata
        pins.extend(
            [
                artifact_pin(
                    "planner_bundle",
                    [
                        metadata.planner_id,
                        metadata.planner_version,
                        metadata.planning_model_id,
                    ],
                    ArtifactKind.CHECKPOINT,
                ),
                artifact_pin(
                    "runtime_schema",
                    frozen.checkpoint.schema_version,
                    ArtifactKind.CHECKPOINT,
                ),
            ]
        )
    else:
        pins.extend(
            (
                _unobservable("planner_bundle", frozen, ArtifactKind.CHECKPOINT),
                _unobservable("runtime_schema", frozen, ArtifactKind.CHECKPOINT),
            )
        )
    if frozen.verification is not None:
        pins.append(
            artifact_pin(
                "verification_bundle",
                [
                    frozen.verification.policy_hash,
                    frozen.verification.model_bundle_hash,
                ],
                ArtifactKind.VERIFICATION,
            )
        )
    else:
        pins.append(
            _unobservable("verification_bundle", frozen, ArtifactKind.VERIFICATION)
        )
    return tuple(sorted(pins, key=lambda item: (item.pin_id, item.pin_version)))


def _unobservable(
    pin_id: str, frozen: FrozenRunArtifacts, artifact: ArtifactKind
) -> SUTPinObservation:
    ref = next(
        item for item in frozen.manifest.artifacts if item.artifact_kind is artifact
    )
    return SUTPinObservation(
        pin_id=pin_id,
        pin_version="1",
        status=SUTPinStatus.UNOBSERVABLE,
        reason_code=(
            "artifact_absent"
            if ref.availability is ArtifactAvailability.ABSENT
            else "artifact_corrupt"
        ),
        supporting_ref=SUTPinSupportRef(
            kind=SUTPinSupportKind.ARTIFACT,
            input_manifest_hash=frozen.manifest.manifest_hash,
            artifact_kind=artifact,
        ),
    )


def validate_sut_homogeneity(
    *,
    commit_sha: str,
    system_version: str,
    observations_by_case: tuple[tuple[SUTPinObservation, ...], ...],
    identity_manifest_hash: str = "0" * 64,
) -> SystemUnderTest:
    identity = (
        ("researchos_commit", commit_sha),
        ("system_version", system_version),
    )
    declared_identity = tuple(
        SUTPinObservation(
            pin_id=pin_id,
            pin_version="1",
            value_hash=stable_hash(value),
            status=SUTPinStatus.DECLARED,
            supporting_ref=SUTPinSupportRef(
                kind=SUTPinSupportKind.DECLARATION,
                input_manifest_hash=identity_manifest_hash,
            ),
        )
        for pin_id, value in identity
    )
    grouped: dict[tuple[str, str], list[SUTPinObservation]] = {}
    for observations in (*observations_by_case, declared_identity):
        for item in observations:
            grouped.setdefault((item.pin_id, item.pin_version), []).append(item)
    versions: dict[str, set[str]] = {}
    for pin_id, pin_version in grouped:
        versions.setdefault(pin_id, set()).add(pin_version)
    if any(len(items) > 1 for items in versions.values()):
        raise SUTHomogeneityError("SUT pin version conflict")
    canonical: list[SUTPinObservation] = []
    any_verified = False
    downgraded = False
    for key in sorted(grouped):
        values = grouped[key]
        observed_hashes = {
            item.value_hash
            for item in values
            if item.status in {SUTPinStatus.ARTIFACT_VERIFIED, SUTPinStatus.DECLARED}
        }
        if len(observed_hashes) > 1:
            raise SUTHomogeneityError(f"SUT pin value conflict for {key[0]}")
        any_verified |= any(
            item.status is SUTPinStatus.ARTIFACT_VERIFIED for item in values
        )
        downgraded |= any(
            item.status is not SUTPinStatus.ARTIFACT_VERIFIED for item in values
        )
        canonical.append(
            sorted(
                values,
                key=lambda item: (
                    list(SUTPinStatus).index(item.status),
                    item.value_hash or "",
                ),
            )[0]
        )
    grade = (
        ProvenanceGrade.ARTIFACT_VERIFIED
        if any_verified and not downgraded
        else ProvenanceGrade.PARTIALLY_VERIFIED
        if any_verified
        else ProvenanceGrade.DECLARED
    )
    provisional = SystemUnderTest.model_construct(
        commit_sha=commit_sha,
        system_version=system_version,
        observations=tuple(canonical),
        provenance_grade=grade,
        system_hash="0" * 64,
    )
    return SystemUnderTest(
        **provisional.model_dump(exclude={"system_hash"}),
        system_hash=stable_hash(
            provisional.model_dump(mode="json", exclude={"system_hash"})
        ),
    )
