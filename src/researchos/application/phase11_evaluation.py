"""Read-only Phase 11 projections over the existing Phase 7 artifact freeze."""

from __future__ import annotations

from dataclasses import dataclass

from researchos.application.evaluation_artifacts import FrozenRunArtifacts
from researchos.application.evaluation_evaluators import _definition
from researchos.domain.evaluation import (
    AggregationKind,
    BooleanMetricValue,
    MetricCertainty,
    MetricDirection,
    MetricEvidenceProvenance,
    MetricEvidenceRef,
    MetricLayer,
    MetricValueKind,
    NonComputedMetricValue,
)
from researchos.domain.runtime import UsageCertainty


@dataclass(frozen=True)
class Phase11MetricProjection:
    """Values derived in memory; no measurement store or evaluator authority."""

    evidence_count: int | None
    claim_count: int | None
    completed_task_count: int | None
    cost_microunits: int | None
    cost_certainty: UsageCertainty | None
    verification_available: bool


class Phase11OperationalEvaluator:
    """Bounded Phase 7 evaluator for durable operational facts only."""

    evaluator_id = "phase11_operational"
    evaluator_version = "1"
    definitions = (
        _definition(
            "phase11_terminal",
            MetricLayer.EXECUTION,
            MetricValueKind.BOOLEAN,
            "boolean",
            MetricDirection.HIGHER_IS_BETTER,
            AggregationKind.BOOLEAN_RATE,
            evaluator_id,
        ),
        _definition(
            "phase11_total_latency",
            MetricLayer.EXECUTION,
            MetricValueKind.INTEGER,
            "milliseconds",
            MetricDirection.INFORMATIONAL,
            AggregationKind.NUMERIC,
            evaluator_id,
        ),
    )

    def evaluate(self, case, frozen: FrozenRunArtifacts):
        del case
        state = frozen.run_state
        if state is None:
            return tuple(
                NonComputedMetricValue(
                    metric_id=item.metric_id,
                    definition_hash=item.definition_hash,
                    status="unavailable",
                    reason_code="run_state_absent",
                )
                for item in self.definitions
            )
        ref = MetricEvidenceRef(
            artifact_kind="run_state",
            input_manifest_hash=frozen.manifest.manifest_hash,
            field_selector="status",
            provenance=MetricEvidenceProvenance.AUTHORITATIVE,
            algorithm_version="phase11-operational-v1",
        )
        terminal = state.status.value in {"completed", "partial", "failed", "cancelled"}
        elapsed = int((state.updated_at - state.created_at).total_seconds() * 1000)
        return (
            BooleanMetricValue(
                metric_id="phase11_terminal",
                definition_hash=self.definitions[0].definition_hash,
                value=terminal,
                certainty=MetricCertainty.EXACT,
                evidence_refs=(ref,),
            ),
            __import__(
                "researchos.domain.evaluation", fromlist=["IntegerMetricValue"]
            ).IntegerMetricValue(
                metric_id="phase11_total_latency",
                definition_hash=self.definitions[1].definition_hash,
                value=elapsed,
                certainty=MetricCertainty.EXACT,
                evidence_refs=(ref,),
            ),
        )


def project_phase11_metrics(artifacts: FrozenRunArtifacts) -> Phase11MetricProjection:
    """Preserve absence/unknown values instead of converting them to zero."""

    checkpoint = artifacts.checkpoint
    evidence = artifacts.evidence
    claims = artifacts.claims
    state = artifacts.run_state
    completed = (
        None
        if checkpoint is None
        else sum(item.status.value == "succeeded" for item in checkpoint.task_states)
    )
    # RunState contains an aggregate budget amount but no durable provider
    # settlement certainty.  It must not be promoted to observed EXACT cost.
    del state
    return Phase11MetricProjection(
        evidence_count=None if evidence is None else len(evidence.evidence),
        claim_count=None if claims is None else len(claims.claims),
        completed_task_count=completed,
        cost_microunits=None,
        cost_certainty=UsageCertainty.UNKNOWN,
        verification_available=artifacts.verification is not None,
    )
