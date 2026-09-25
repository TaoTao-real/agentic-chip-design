from __future__ import annotations

from typing import Any, Iterable, Mapping

from .schema import Measurement, SchemaError


COMPARISON_RECIPE_REVISION = "comparable_delta_v1"
METRIC_DEFINITIONS = {
    "critical_delay_ns": ("timing-critical-delay-v1", "ns"),
    "clock_period_ns": ("clock-constraint-v1", "ns"),
    "wns_ns": ("timing-wns-v1", "ns"),
    "tns_ns": ("timing-tns-v1", "ns"),
    "failing_endpoints": ("timing-failing-endpoints-v1", "count"),
    "total_endpoints": ("timing-total-endpoints-v1", "count"),
    "slice_luts": ("vivado-slice-luts-v1", "count"),
    "slice_registers": ("vivado-slice-registers-v1", "count"),
}
STAGE_MAP = {
    "materialize": "materialize",
    "elaboration": "elaboration",
    "correctness": "correctness",
    "synthesis": "post_synth",
    "post_synth": "post_synth",
    "route": "post_route",
    "post_route": "post_route",
    "regression": "regression",
}
COMPARISON_CONDITION_KEYS = (
    "stage",
    "part",
    "clock_period_ns",
    "tool_fingerprint",
    "reference_fingerprint",
)


def measurement_from_value(value: Measurement | Mapping[str, Any]) -> Measurement:
    """Validate a measurement without changing its evidence representation."""
    if isinstance(value, Measurement):
        return value
    if not isinstance(value, Mapping):
        raise SchemaError("measurement must be an object")
    try:
        return Measurement(**dict(value))
    except TypeError as exc:
        raise SchemaError("measurement has an invalid shape") from exc


def metric_map(value: Mapping[str, Any]) -> dict[str, Any]:
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else value
    return {key: metrics[key] for key in METRIC_DEFINITIONS if key in metrics}


def baseline_measurement(
    baseline: Mapping[str, Any],
    metric_id: str,
    source_ref: str,
    conditions: dict[str, Any],
) -> Measurement:
    raw = metric_map(baseline)[metric_id]
    definition_revision, unit = METRIC_DEFINITIONS[metric_id]
    if isinstance(raw, dict):
        value = raw.get("value")
        definition_revision = str(raw.get("definition_revision", definition_revision))
        unit = str(raw.get("unit", unit))
    else:
        value = raw
    return Measurement(
        metric_id=metric_id,
        definition_revision=definition_revision,
        value=value,
        unit=unit,
        scope=conditions,
        provenance="legacy_baseline_record",
        source_ref=source_ref,
        availability="available",
        mapping_quality="exact",
    )


def comparison_conditions(measurements: Iterable[Measurement]) -> dict[str, Any]:
    """Return one condition set or an explicit inconclusive marker.

    Measurements for one evaluated stage are expected to share a scope.  A
    disagreement is evidence conflict; callers must not silently select one.
    """
    scopes = [
        {key: value.scope.get(key) for key in COMPARISON_CONDITION_KEYS}
        for value in measurements
    ]
    if not scopes:
        return {key: None for key in COMPARISON_CONDITION_KEYS}
    first = scopes[0]
    if any(scope != first for scope in scopes[1:]):
        return {key: None for key in COMPARISON_CONDITION_KEYS}
    return first


def compare_metric(
    reference: Measurement | None,
    current: Measurement | None,
    *,
    metric_id: str | None = None,
    reference_conditions: Mapping[str, Any],
    current_conditions: Mapping[str, Any],
    conflicted: bool = False,
    ineligibility_reason: str | None = None,
) -> dict[str, Any]:
    """Apply the shared CC-01/CC-02 comparability rules to one metric."""
    metric_id = metric_id or (
        current.metric_id if current is not None
        else reference.metric_id if reference is not None
        else None
    )
    if metric_id is None:
        raise SchemaError("a metric comparison needs at least one measurement")
    result: dict[str, Any] = {
        "metric_id": metric_id,
        "status": None,
        "reference": reference.to_dict() if reference is not None else None,
        "current": current.to_dict() if current is not None else None,
        "delta": None,
        "reason": None,
    }
    if conflicted:
        result.update(status="conflict", reason="conflicting_sources")
        return result
    if ineligibility_reason is not None:
        result.update(status="not_comparable", reason=ineligibility_reason)
        return result
    if reference is None or current is None:
        result.update(
            status="missing",
            reason=(
                "reference_metric_missing" if reference is None
                else "current_metric_missing"
            ),
        )
        return result
    if reference.availability != "available" or current.availability != "available":
        result.update(status="missing", reason="measurement_unavailable")
        return result
    missing_conditions = [
        key for key in COMPARISON_CONDITION_KEYS
        if reference_conditions.get(key) is None or current_conditions.get(key) is None
    ]
    if missing_conditions:
        result.update(
            status="not_comparable",
            reason="comparison_condition_missing",
            missing_conditions=missing_conditions,
        )
        return result
    differing_conditions = [
        key for key in COMPARISON_CONDITION_KEYS
        if reference_conditions.get(key) != current_conditions.get(key)
    ]
    if differing_conditions:
        result.update(
            status="not_comparable",
            reason="comparison_condition_differs",
            differing_conditions=differing_conditions,
        )
        return result
    if reference.definition_revision != current.definition_revision:
        result.update(status="not_comparable", reason="definition_revision_differs")
        return result
    if reference.unit != current.unit:
        result.update(status="not_comparable", reason="unit_differs")
        return result
    result.update(
        status="comparable",
        delta=current.value - reference.value,
        reason=None,
    )
    return result


def compare_metric_sets(
    reference: Mapping[str, Measurement],
    current: Mapping[str, Measurement],
    *,
    metric_ids: Iterable[str],
    reference_conditions: Mapping[str, Any],
    current_conditions: Mapping[str, Any],
    conflicted_metric_ids: Iterable[str] = (),
    ineligibility_reason: str | None = None,
) -> list[dict[str, Any]]:
    conflicts = set(conflicted_metric_ids)
    return [
        compare_metric(
            reference.get(metric_id),
            current.get(metric_id),
            metric_id=metric_id,
            reference_conditions=reference_conditions,
            current_conditions=current_conditions,
            conflicted=metric_id in conflicts,
            ineligibility_reason=ineligibility_reason,
        )
        for metric_id in metric_ids
    ]


def legacy_delta_rows(comparisons: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Render comparable results in the byte-for-byte CC-01 bundle shape."""
    rows: list[dict[str, Any]] = []
    for comparison in comparisons:
        if comparison.get("status") != "comparable":
            continue
        reference = comparison["reference"]
        current = comparison["current"]
        rows.append({
            "metric_id": comparison["metric_id"],
            "definition_revision": reference["definition_revision"],
            "unit": reference["unit"],
            "baseline": reference["value"],
            "current": current["value"],
            "delta": comparison["delta"],
        })
    return rows


__all__ = [
    "COMPARISON_CONDITION_KEYS",
    "COMPARISON_RECIPE_REVISION",
    "METRIC_DEFINITIONS",
    "STAGE_MAP",
    "baseline_measurement",
    "compare_metric",
    "compare_metric_sets",
    "comparison_conditions",
    "legacy_delta_rows",
    "measurement_from_value",
    "metric_map",
]
