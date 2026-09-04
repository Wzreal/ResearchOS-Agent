"""Frozen local-trace to optional-observation projection."""

from __future__ import annotations

from researchos.domain.contracts import TraceEvent, TraceEventType
from researchos.domain.identity import stable_id
from researchos.domain.observability import ObservationEnvelope, ObservationScope
from researchos.domain.runtime import TraceEventDescriptor
from researchos.security.redaction import PersistenceRedactor

_LOCAL_ONLY = frozenset(
    {
        TraceEventType.OBSERVABILITY_EXPORT_FAILED,
        TraceEventType.OBSERVABILITY_DELIVERY_DROPPED,
        TraceEventType.OBSERVABILITY_OPTIONAL_OMITTED,
    }
)


def _scope(event_type: TraceEventType) -> ObservationScope:
    value = event_type.value
    if event_type in _LOCAL_ONLY:
        return ObservationScope.OBSERVABILITY
    if value.startswith("evidence."):
        return ObservationScope.EVIDENCE
    if value.startswith("claim."):
        return ObservationScope.CLAIM
    if value.startswith("evaluation."):
        return ObservationScope.EVALUATION
    if value.startswith("verification.operation_recovery"):
        return ObservationScope.RECOVERY
    if value.startswith("verification.") or value.startswith("synthesis."):
        return ObservationScope.VERIFICATION
    if value.startswith("tool."):
        return ObservationScope.TOOL
    if value.startswith("agent.") or value.startswith("runtime.attempt"):
        return ObservationScope.ATTEMPT
    if value.startswith("runtime.task"):
        return ObservationScope.TASK
    return ObservationScope.RUN


def _safe_id(attributes: dict[str, object], key: str) -> str | None:
    """Read an identity only from an explicitly frozen key."""

    value = attributes.get(key)
    if not isinstance(value, str):
        return None
    # SafeId validation is supplied by ObservationEnvelope, but reject values
    # that would make the projection itself non-deterministically fail later.
    return value if value and len(value) <= 80 else None


def project_trace_event(
    event: TraceEvent | TraceEventDescriptor,
) -> ObservationEnvelope:
    """Project exact local event identity through a fixed allowlist only.

    This boundary deliberately does not iterate arbitrary attributes to infer
    resource identity.  Exporters receive the envelope, never a heuristic
    attribute scan.
    """

    descriptor = (
        event
        if isinstance(event, TraceEventDescriptor)
        else TraceEventDescriptor.from_event(event)
    )
    PersistenceRedactor().assert_safe_model(descriptor)
    scope = _scope(descriptor.event_type)
    attrs = descriptor.attributes
    ids: dict[str, str | None] = {
        "task_id": _safe_id(attrs, "task_id"),
        "attempt_id": _safe_id(attrs, "attempt_id"),
        "agent_id": _safe_id(attrs, "agent_id"),
        "tool_call_id": _safe_id(attrs, "tool_call_id"),
        "evidence_id": _safe_id(attrs, "evidence_id"),
        "claim_id": _safe_id(attrs, "claim_id"),
        "verification_id": None,
        "evaluation_id": None,
    }
    if scope in {ObservationScope.VERIFICATION, ObservationScope.RECOVERY}:
        ids["verification_id"] = descriptor.correlation_id
    elif scope is ObservationScope.EVALUATION:
        ids["evaluation_id"] = descriptor.correlation_id
    elif scope is ObservationScope.ATTEMPT or scope is ObservationScope.TOOL:
        ids["attempt_id"] = ids["attempt_id"] or descriptor.correlation_id
    return ObservationEnvelope(
        envelope_id=stable_id(
            "obs", [descriptor.event_id, descriptor.canonical_event_hash]
        ),
        scope=scope,
        descriptor=descriptor,
        export=descriptor.event_type not in _LOCAL_ONLY,
        **ids,
    )
