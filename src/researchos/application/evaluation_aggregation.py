"""Deterministic aggregation of typed Phase 7 metrics."""

from __future__ import annotations

from decimal import Decimal

from researchos.domain.evaluation import (
    BooleanMetricValue,
    EnumMetricValue,
    FloatMetricValue,
    IntegerMetricValue,
    MetricAggregate,
    MetricDefinitionSnapshot,
    MetricStatus,
    MetricValue,
    RatioMetricValue,
    normalize_metric_decimal_v1,
)


def _mean(values: list[float]) -> float:
    total = sum(Decimal(str(item)) for item in values)
    return normalize_metric_decimal_v1(total / len(values))


def _median(values: list[float]) -> float:
    ordered = sorted(Decimal(str(item)) for item in values)
    middle = len(ordered) // 2
    value = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return normalize_metric_decimal_v1(value)


def aggregate_metrics(
    metrics_by_case: tuple[tuple[MetricValue, ...], ...],
    definitions: tuple[MetricDefinitionSnapshot, ...],
) -> tuple[MetricAggregate, ...]:
    result: list[MetricAggregate] = []
    for definition in sorted(definitions, key=lambda item: item.metric_id):
        values = [
            item
            for metrics in metrics_by_case
            for item in metrics
            if item.metric_id == definition.metric_id
        ]
        statuses = {
            status: sum(item.status is status for item in values)
            for status in MetricStatus
        }
        numeric: list[float] = []
        ratios: list[RatioMetricValue] = []
        distribution: dict[str, int] = {}
        for item in values:
            if isinstance(item, (IntegerMetricValue, FloatMetricValue)):
                numeric.append(float(item.value))
            elif isinstance(item, RatioMetricValue):
                numeric.append(item.value)
                ratios.append(item)
            elif isinstance(item, BooleanMetricValue):
                numeric.append(1.0 if item.value else 0.0)
            elif isinstance(item, EnumMetricValue):
                distribution[item.value] = distribution.get(item.value, 0) + 1
        micro = None
        if ratios:
            denominator = sum(item.denominator for item in ratios)
            if denominator:
                micro = normalize_metric_decimal_v1(
                    Decimal(sum(item.numerator for item in ratios)) / denominator
                )
        result.append(
            MetricAggregate(
                metric_id=definition.metric_id,
                definition_hash=definition.definition_hash,
                computed_count=statuses[MetricStatus.COMPUTED],
                unavailable_count=statuses[MetricStatus.UNAVAILABLE],
                not_applicable_count=statuses[MetricStatus.NOT_APPLICABLE],
                skipped_count=statuses[MetricStatus.SKIPPED],
                error_count=statuses[MetricStatus.ERROR],
                mean=_mean(numeric) if numeric else None,
                median=_median(numeric) if numeric else None,
                minimum=normalize_metric_decimal_v1(min(numeric)) if numeric else None,
                maximum=normalize_metric_decimal_v1(max(numeric)) if numeric else None,
                micro_ratio=micro,
                enum_distribution=dict(sorted(distribution.items())),
            )
        )
    return tuple(result)
