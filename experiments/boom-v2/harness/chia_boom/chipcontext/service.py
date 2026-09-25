from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    CandidateRef,
    CheckRecord,
    Measurement,
    SchemaError,
    WorkingState,
    canonical_json,
    content_hash,
    hashed_record,
    require_sha256,
)
from .extraction import ExtractionService
from .store import EvidenceStore


PARSER_REVISION = "legacy-evaluation-v3"
POLICY_REVISION = "cc01-static-required-v1"
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


def _artifact_for(
    refs: dict[str, Any], labels: tuple[str, ...]
) -> Any | None:
    for label in labels:
        if label in refs:
            return refs[label]
    for ref in refs.values():
        if ref.kind in labels:
            return ref
    return None


def _raw_extractions(
    *,
    store: EvidenceStore,
    refs: dict[str, Any],
    stage: str,
    clock_period_ns: Any,
) -> list[dict[str, Any]]:
    """Extract facts only when a request explicitly registers raw tool roles."""
    extraction = ExtractionService(
        store, allowed_access={"public", "controlled"}
    )
    records: list[dict[str, Any]] = []
    prefix = stage if stage in {"post_synth", "post_route"} else "post_synth"
    summary = _artifact_for(refs, (
        f"{prefix}_timing_summary", "vivado_timing_summary"
    ))
    utilization = _artifact_for(refs, (
        f"{prefix}_utilization", "vivado_utilization"
    ))
    timing_paths = _artifact_for(refs, (
        f"{prefix}_timing_paths", "vivado_timing_paths"
    ))
    if summary is not None or utilization is not None or timing_paths is not None:
        if summary is None or utilization is None:
            raise SchemaError(
                "raw Vivado extraction requires timing summary and utilization"
            )
        if not isinstance(clock_period_ns, (int, float)):
            raise SchemaError("raw Vivado extraction requires a clock period")
        records.append(extraction.vivado(
            timing_summary_ref=summary.ref_id,
            utilization_ref=utilization.ref_id,
            timing_paths_ref=timing_paths.ref_id if timing_paths else None,
            period_ns=float(clock_period_ns),
            stage=prefix,
        ))
    differential_result = _artifact_for(refs, (
        "differential_result", "verilator_differential_result"
    ))
    differential_stdout = _artifact_for(refs, (
        "differential_stdout", "verilator_differential_stdout"
    ))
    if differential_result is not None:
        records.append(extraction.differential(
            result_ref=differential_result.ref_id,
            stdout_ref=(
                differential_stdout.ref_id if differential_stdout is not None else None
            ),
        ))
    return records


def _reconcile_extractions(
    *,
    checks: list[CheckRecord],
    measurements: list[Measurement],
    records: list[dict[str, Any]],
    conditions: dict[str, Any],
    conflicts: list[dict[str, Any]],
    missing: list[dict[str, str]],
) -> tuple[list[CheckRecord], list[Measurement], dict[str, list[str]], set[str]]:
    """Merge compatible raw/legacy facts and surface every disagreement."""
    check_index = {value.check_id: index for index, value in enumerate(checks)}
    metric_index = {value.metric_id: value for value in measurements}
    sources: dict[str, list[str]] = {
        value.metric_id: [value.source_ref] for value in measurements
    }
    conflict_metrics: set[str] = set()
    for record in records:
        extraction_ref = record["content_hash"]
        missing.extend(record.get("missing", []))
        conflicts.extend(record.get("conflicts", []))
        facts = record.get("facts", {})
        for raw in facts.get("metrics", []):
            metric_id = raw.get("metric_id")
            if not isinstance(metric_id, str):
                continue
            legacy = metric_index.get(metric_id)
            if legacy is None:
                scope = {"stage": raw.get("stage"), **conditions}
                value = Measurement(
                    metric_id=metric_id,
                    definition_revision=str(raw.get("definition_revision")),
                    value=raw.get("value"),
                    unit=str(raw.get("unit")),
                    scope=scope,
                    provenance="verified_raw_tool_report",
                    source_ref=extraction_ref,
                    availability="available",
                    mapping_quality="exact",
                )
                measurements.append(value)
                metric_index[metric_id] = value
                sources[metric_id] = [extraction_ref]
                continue
            same = (
                legacy.definition_revision == raw.get("definition_revision")
                and legacy.unit == raw.get("unit")
                and legacy.scope.get("stage") == raw.get("stage")
                and float(legacy.value) == float(raw.get("value"))
            )
            if same:
                sources.setdefault(metric_id, [legacy.source_ref]).append(extraction_ref)
            else:
                conflict_metrics.add(metric_id)
                conflicts.append({
                    "field": f"measurements.{metric_id}",
                    "reason": "conflicting_sources",
                    "detail": (
                        "raw and legacy metric differ in value, unit, definition, "
                        "or stage"
                    ),
                    "source_refs": [legacy.source_ref, extraction_ref],
                })
        raw_check = facts.get("check")
        if isinstance(raw_check, dict):
            check_id = raw_check.get("check_id")
            index = check_index.get(check_id)
            if index is not None:
                legacy = checks[index]
                raw_outcome = raw_check.get("outcome")
                if not legacy.executed and raw_outcome in {
                    "pass", "fail", "inconclusive"
                }:
                    checks[index] = CheckRecord(
                        check_id=legacy.check_id,
                        executed=True,
                        outcome=raw_outcome,
                        scope={"raw_extractor": record["extractor"]["revision"]},
                        source_ref=extraction_ref,
                    )
                elif legacy.executed and legacy.outcome != raw_outcome:
                    conflicts.append({
                        "field": f"checks.{check_id}",
                        "reason": "conflicting_sources",
                        "detail": (
                            f"legacy={legacy.outcome}, raw={raw_outcome}"
                        ),
                        "source_refs": [legacy.source_ref, extraction_ref],
                    })
                    checks[index] = CheckRecord(
                        check_id=legacy.check_id,
                        executed=True,
                        outcome="inconclusive",
                        scope=legacy.scope,
                        source_ref=legacy.source_ref,
                    )
    for values in sources.values():
        values[:] = sorted(set(values))
    return checks, measurements, sources, conflict_metrics


@dataclass(frozen=True)
class PreparationResult:
    evaluation_manifest: dict[str, Any]
    snapshot: dict[str, Any]
    bundle: dict[str, Any]
    packet: dict[str, Any]
    markdown: str

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": "chipcontext.preparation-result.v1",
            "evaluation_manifest": self.evaluation_manifest["content_hash"],
            "snapshot": self.snapshot["content_hash"],
            "bundle": self.bundle["content_hash"],
            "packet": self.packet["content_hash"],
            "packet_bytes": len(canonical_json(self.packet).encode()),
        }


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read {label}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be a JSON object")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sealed_manifest(path: Path) -> dict[str, Any]:
    value = _load_object(path, "sealed manifest")
    fingerprint = value.get("fingerprint")
    require_sha256("sealed manifest fingerprint", fingerprint or "")
    body = {key: item for key, item in value.items() if key != "fingerprint"}
    if content_hash(body) != fingerprint:
        raise SchemaError("sealed manifest fingerprint is invalid")
    return value


def _evaluation_from_result(value: dict[str, Any]) -> dict[str, Any]:
    evaluation = value.get("search_evaluation") or value.get("evaluation") or value
    if not isinstance(evaluation, dict):
        raise SchemaError("legacy result does not contain an evaluation object")
    return evaluation


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_evaluation_binding(
    *,
    result_data: dict[str, Any],
    evaluation: dict[str, Any],
    candidate_path: Path,
    candidate_id: str,
    experiment_id: str,
    source_sha256: str,
    attempt_id: str,
) -> None:
    """Fail closed unless the legacy result identifies source and attempt.

    New row-style results carry the complete CandidateArtifact plus an explicit
    attempt ID.  Older standalone EvaluationArtifact files are accepted only
    when their recorded candidate-source path is inside the candidate directory,
    contains the requested attempt component, and hashes to the candidate source.
    """
    evaluated_id = evaluation.get("candidate_id")
    if not isinstance(evaluated_id, str) or not evaluated_id:
        raise SchemaError("evaluation candidate ID is required")
    if evaluated_id != candidate_id:
        raise SchemaError("evaluation candidate ID does not match candidate record")

    embedded = result_data.get("candidate")
    embedded_attempt = result_data.get("attempt_id")
    if isinstance(embedded, dict) and isinstance(embedded.get("source"), str):
        if str(embedded.get("id") or "") != candidate_id:
            raise SchemaError("embedded evaluation candidate ID does not match")
        if str(embedded.get("campaign_id") or "") != experiment_id:
            raise SchemaError("embedded evaluation campaign does not match")
        embedded_sha = _sha256_text(embedded["source"])
        if embedded.get("source_sha256") not in (None, embedded_sha):
            raise SchemaError("embedded evaluation source hash is invalid")
        if embedded_sha != source_sha256:
            raise SchemaError("evaluation source does not match candidate source")
        if embedded_attempt is not None and embedded_attempt != attempt_id:
            raise SchemaError("evaluation attempt does not match requested attempt")
        if embedded_attempt == attempt_id:
            return

    raw_source_path = evaluation.get("candidate_source_path")
    if not isinstance(raw_source_path, str) or not raw_source_path:
        raise SchemaError("evaluation source binding is required")
    source_path = Path(raw_source_path)
    if not source_path.is_absolute() or source_path.is_symlink():
        raise SchemaError("legacy evaluation source binding is invalid")
    resolved_source = source_path.resolve(strict=True)
    candidate_root = candidate_path.parent.resolve(strict=True)
    try:
        relative = resolved_source.relative_to(candidate_root)
    except ValueError as exc:
        raise SchemaError("legacy evaluation source escapes candidate attempt") from exc
    if attempt_id not in relative.parts:
        raise SchemaError("evaluation attempt does not match requested attempt")
    cursor = candidate_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SchemaError("legacy evaluation source traverses a symlink")
    if _sha256_file(resolved_source) != source_sha256:
        raise SchemaError("evaluation source does not match candidate source")


def _verify_manifest_binding(
    *,
    role: str,
    artifact_path: Path | None,
    request: dict[str, Any],
    sealed: dict[str, Any],
    missing: list[dict[str, str]],
) -> bool:
    bindings = request.get("manifest_bindings")
    logical_path = bindings.get(role) if isinstance(bindings, dict) else None
    files = sealed.get("files")
    expected = files.get(logical_path) if isinstance(files, dict) else None
    if artifact_path is None or not isinstance(logical_path, str) or not logical_path:
        missing.append({
            "field": f"bindings.{role}",
            "reason": "not_collected",
            "detail": "request has no explicit sealed-manifest artifact binding",
        })
        return False
    if not isinstance(expected, str):
        missing.append({
            "field": f"bindings.{role}",
            "reason": "not_comparable",
            "detail": f"sealed manifest has no file entry for {logical_path!r}",
        })
        return False
    require_sha256(f"sealed manifest files[{logical_path!r}]", expected)
    if _sha256_file(artifact_path) != expected:
        missing.append({
            "field": f"bindings.{role}",
            "reason": "not_comparable",
            "detail": "artifact bytes do not match the sealed-manifest file entry",
        })
        return False
    return True


def _stage_executed(evaluation: dict[str, Any], names: set[str]) -> bool:
    return any(
        isinstance(row, dict) and row.get("stage") in names
        for row in evaluation.get("stages", [])
    )


def _stage_outcome(evaluation: dict[str, Any], names: set[str]) -> bool | None:
    rows = [
        row for row in evaluation.get("stages", [])
        if isinstance(row, dict) and row.get("stage") in names
    ]
    if (
        not rows
        or "success" not in rows[-1]
        or rows[-1].get("status") == "infra_blocked"
    ):
        return None
    return bool(rows[-1]["success"])


def _stage_infra_blocked(evaluation: dict[str, Any], names: set[str]) -> bool:
    rows = [
        row for row in evaluation.get("stages", [])
        if isinstance(row, dict) and row.get("stage") in names
    ]
    return bool(rows and rows[-1].get("status") == "infra_blocked")


def _checks(
    evaluation: dict[str, Any], source_ref: str,
) -> tuple[list[CheckRecord], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    differential = evaluation.get("differential")
    build_stage = _stage_outcome(evaluation, {"elaboration"})
    correctness_stage = _stage_outcome(evaluation, {"correctness"})
    synth_stage = _stage_outcome(evaluation, {"synthesis", "post_synth"})
    route_stage = _stage_outcome(evaluation, {"route", "post_route"})
    regression_stage = _stage_outcome(evaluation, {"regression"})

    # A stage entry proves that work was attempted.  It does not prove a
    # functional pass/fail when the producer omitted success (for example an
    # infrastructure interruption).  Dataclass default booleans are summaries,
    # not execution evidence, and are only reconciled with concrete payloads.
    build_executed = _stage_executed(evaluation, {"elaboration"})
    correctness_executed = isinstance(differential, dict) or _stage_executed(
        evaluation, {"correctness"}
    )
    interface_executed = isinstance(differential, dict) and (
        "interface_ok" in differential
    )
    synth_executed = isinstance(evaluation.get("post_synth"), dict) or _stage_executed(
        evaluation, {"synthesis", "post_synth"}
    )
    route_executed = isinstance(evaluation.get("post_route"), dict) or _stage_executed(
        evaluation, {"route", "post_route"}
    )
    regression_executed = isinstance(evaluation.get("regression"), dict) or _stage_executed(
        evaluation, {"regression"}
    )

    def record(check_id: str, executed: bool, passed: bool | None) -> CheckRecord:
        outcome = None
        if executed:
            outcome = "inconclusive" if passed is None else ("pass" if passed else "fail")
        return CheckRecord(
            check_id=check_id,
            executed=executed,
            outcome=outcome,
            scope={"legacy_field_mapping": PARSER_REVISION},
            source_ref=source_ref if executed else None,
        )

    def reconcile(
        check_id: str, sources: dict[str, bool | None],
    ) -> bool | None:
        known = {
            name: value for name, value in sources.items()
            if isinstance(value, bool)
        }
        if not known:
            return None
        if len(set(known.values())) > 1:
            conflicts.append({
                "field": f"checks.{check_id}",
                "reason": "conflicting_sources",
                "detail": ", ".join(
                    f"{name}={str(value).lower()}"
                    for name, value in sorted(known.items())
                ),
                "source_ref": source_ref,
            })
            return None
        return next(iter(known.values()))

    regression = evaluation.get("regression")
    regression_payload: bool | None = None
    if (
        isinstance(regression, dict)
        and not _stage_infra_blocked(evaluation, {"regression"})
    ):
        if isinstance(regression.get("passed"), bool):
            regression_payload = regression["passed"]
        elif isinstance(regression.get("success"), bool) and isinstance(
            regression.get("returncode"), int
        ):
            regression_payload = bool(
                regression["success"] and regression["returncode"] == 0
            )
    differential_payload = (
        bool(differential["passed"])
        if isinstance(differential, dict)
        and isinstance(differential.get("passed"), bool)
        and not _stage_infra_blocked(evaluation, {"correctness"})
        else None
    )
    interface_payload = (
        bool(differential["interface_ok"])
        if isinstance(differential, dict)
        and isinstance(differential.get("interface_ok"), bool)
        and not _stage_infra_blocked(evaluation, {"correctness"})
        else None
    )
    synth_payload = (
        True
        if isinstance(evaluation.get("post_synth"), dict)
        and not _stage_infra_blocked(evaluation, {"synthesis", "post_synth"})
        else None
    )
    route_payload = (
        True
        if isinstance(evaluation.get("post_route"), dict)
        and not _stage_infra_blocked(evaluation, {"route", "post_route"})
        else None
    )

    # Default summary False fields do not prove execution. Once a concrete
    # payload proves the predicate was evaluated, however, every available
    # synonymous source must agree before the check can be certified.
    build_passed = reconcile(
        "elaboration",
        {
            "summary": bool(evaluation["build_ok"])
            if build_stage is not None
            and isinstance(evaluation.get("build_ok"), bool)
            else None,
            "stage": build_stage,
        },
    )
    correctness_passed = reconcile(
        "differential_correctness",
        {
            "payload": differential_payload,
            "summary": bool(evaluation["correctness_ok"])
            if differential_payload is not None
            and isinstance(evaluation.get("correctness_ok"), bool)
            else None,
            "stage": correctness_stage,
        },
    )
    interface_passed = reconcile(
        "interface_signature",
        {
            "payload": interface_payload,
            "summary": bool(evaluation["interface_ok"])
            if interface_payload is not None
            and isinstance(evaluation.get("interface_ok"), bool)
            else None,
        },
    )
    synth_passed = reconcile(
        "post_synth", {"payload": synth_payload, "stage": synth_stage}
    )
    route_passed = reconcile(
        "post_route", {"payload": route_payload, "stage": route_stage}
    )
    regression_passed = reconcile(
        "processor_regression",
        {"payload": regression_payload, "stage": regression_stage},
    )
    checks = [
        record("elaboration", build_executed, build_passed),
        # A legacy lint_ok=False does not prove lint was executed.
        record("lint", _stage_executed(evaluation, {"lint"}), None),
        record("interface_signature", interface_executed, interface_passed),
        record("differential_correctness", correctness_executed, correctness_passed),
        record("post_synth", synth_executed, synth_passed),
        record("post_route", route_executed, route_passed),
        record("processor_regression", regression_executed, regression_passed),
        record("formal_equivalence", False, None),
    ]
    return checks, conflicts


def _measurements(
    evaluation: dict[str, Any], normalized_stage: str, source_ref: str,
    conditions: dict[str, Any], missing: list[dict[str, str]],
) -> list[Measurement]:
    if normalized_stage not in {"post_synth", "post_route"}:
        missing.append({
            "field": f"measurements.{normalized_stage}",
            "reason": "not_supported",
            "detail": "v1 only normalizes post-synth and post-route PPA",
        })
        return []
    if normalized_stage == "post_route":
        ppa = evaluation.get("post_route")
    else:
        ppa = evaluation.get("post_synth")
    if not isinstance(ppa, dict):
        missing.append({
            "field": f"measurements.{normalized_stage}",
            "reason": "not_collected",
            "detail": "legacy evaluation has no structured PPA payload",
        })
        return []
    values = []
    for metric_id, (revision, unit) in METRIC_DEFINITIONS.items():
        if metric_id not in ppa:
            continue
        values.append(Measurement(
            metric_id=metric_id,
            definition_revision=revision,
            value=ppa[metric_id],
            unit=unit,
            scope={"stage": normalized_stage, **conditions},
            provenance="tool_report_via_legacy_evaluation",
            source_ref=source_ref,
            availability="available",
            mapping_quality="exact",
        ))
    if not values:
        missing.append({
            "field": f"measurements.{normalized_stage}",
            "reason": "parse_failed",
            "detail": "structured PPA payload contains no supported finite metrics",
        })
    return values


def _metric_map(value: dict[str, Any]) -> dict[str, Any]:
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else value
    return {key: metrics[key] for key in METRIC_DEFINITIONS if key in metrics}


def _baseline_measurement(
    baseline: dict[str, Any], metric_id: str, source_ref: str,
    conditions: dict[str, Any],
) -> Measurement:
    raw = _metric_map(baseline)[metric_id]
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


def _render_markdown(packet: dict[str, Any]) -> str:
    lines = [
        "# ChipContext v1",
        "",
        f"- Experiment: `{packet['experiment_id']}`",
        f"- Candidate: `{packet['candidate_ref']['candidate_id']}`",
        f"- Working state: `{packet['current_evaluation_status']}`",
        f"- Bundle kind: `{packet['bundle']['kind']}`",
        f"- Completeness: `{packet['completeness']['status']}`",
        "",
        "## Selected evidence",
        "",
    ]
    for item in packet["selected"]:
        lines.append(f"- **{item['kind']}**: {item['summary']}")
    if packet["missing"]:
        lines.extend(["", "## Missing evidence", ""])
        for item in packet["missing"]:
            lines.append(
                f"- `{item['field']}`: `{item['reason']}` — {item['detail']}"
            )
    if packet["conflicts"]:
        lines.extend(["", "## Conflicting evidence", ""])
        for item in packet["conflicts"]:
            lines.append(
                f"- `{item['field']}`: `{item['reason']}` — {item['detail']}"
            )
    lines.extend(["", "## Drilldown", ""])
    for ref in packet["drilldown_refs"]:
        lines.append(f"- `{ref['kind']}` → `{ref['artifact_ref']}`")
    return "\n".join(lines) + "\n"


def _apply_packet_budget(
    packet_payload: dict[str, Any], max_bytes: int,
) -> tuple[dict[str, Any], int]:
    """Return a payload whose budget covers the final canonical record bytes."""
    if max_bytes < 1:
        raise SchemaError("max_payload_bytes must be positive")
    payload = json.loads(canonical_json(packet_payload))
    required = 0
    for _ in range(8):
        payload["budget"] = {
            "unit": "canonical_record_bytes",
            "maximum": max_bytes,
            "required": required,
            "overflow_policy": "fail_closed",
        }
        record = hashed_record("chipcontext.context-packet.v1", payload)
        actual = len(canonical_json(record).encode())
        if actual == required:
            if actual > max_bytes:
                raise SchemaError(
                    "required_evidence_overflow: "
                    f"required={actual} max={max_bytes}"
                )
            return payload, actual
        required = actual
    raise SchemaError("context packet byte accounting did not converge")


class ChipContextService:
    def __init__(self, store: EvidenceStore):
        self.store = store

    def prepare(self, request_path: Path) -> PreparationResult:
        started = time.monotonic()
        request = _load_object(request_path, "prepare request")
        if request.get("schema_version") != "chipcontext.prepare-request.v1":
            raise SchemaError("unsupported prepare request schema")
        request_root = request_path.parent.resolve()
        declared_root = request.get("input_root", ".")
        if not isinstance(declared_root, str) or Path(declared_root).is_absolute():
            raise SchemaError("input_root must be relative to the request")
        input_root = (request_root / declared_root).resolve(strict=True)
        registered_root = self.store.artifact_roots.get("input")
        if registered_root != input_root:
            raise SchemaError("prepare request input_root does not match store root")

        def path_for(name: str, *, required: bool = True) -> Path | None:
            raw = request.get(name)
            if raw is None and not required:
                return None
            if not isinstance(raw, str) or Path(raw).is_absolute():
                raise SchemaError(f"{name} must be a relative path")
            relative = Path(raw)
            if ".." in relative.parts:
                raise SchemaError(f"{name} escapes the controlled input root")
            cursor = input_root
            for component in relative.parts:
                if component == ".":
                    continue
                cursor = cursor / component
                if cursor.is_symlink():
                    raise SchemaError(f"{name} traverses a symlink")
            resolved = (input_root / relative).resolve(strict=True)
            try:
                resolved.relative_to(input_root)
            except ValueError as exc:
                raise SchemaError(
                    f"{name} escapes the controlled input root"
                ) from exc
            return resolved

        candidate_path = path_for("candidate")
        result_path = path_for("evaluation")
        sealed_path = path_for("sealed_manifest")
        baseline_path = path_for("baseline")
        working_path = path_for("working_source")
        qualification_path = path_for("qualification", required=False)
        assert candidate_path and result_path and sealed_path and baseline_path and working_path

        candidate_data = _load_object(candidate_path, "candidate")
        result_data = _load_object(result_path, "evaluation")
        evaluation = _evaluation_from_result(result_data)
        sealed = _sealed_manifest(sealed_path)
        baseline = _load_object(baseline_path, "baseline")
        qualification = (
            _load_object(qualification_path, "qualification")
            if qualification_path else {}
        )
        source = candidate_data.get("source")
        if not isinstance(source, str):
            raise SchemaError("candidate source is required")
        source_sha = _sha256_text(source)
        recorded_source_sha = candidate_data.get("source_sha256")
        if recorded_source_sha is not None and recorded_source_sha != source_sha:
            raise SchemaError("candidate source hash does not match source bytes")
        parent_ref = None
        if candidate_data.get("parent_id") is not None:
            parent_hash = candidate_data.get("parent_source_sha256")
            require_sha256("parent_source_sha256", parent_hash or "")
            parent_ref = {
                "candidate_id": str(candidate_data["parent_id"]),
                "source_sha256": parent_hash,
            }
        experiment_id = str(request.get("experiment_id") or "")
        candidate_id = str(candidate_data.get("id") or "")
        recorded_experiment = candidate_data.get("campaign_id")
        if recorded_experiment is not None and str(recorded_experiment) != experiment_id:
            raise SchemaError(
                "request experiment ID does not match candidate campaign"
            )
        candidate = CandidateRef(
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            source_sha256=source_sha,
            contract_sha256=sealed["fingerprint"],
            parent_ref=parent_ref,
        )
        attempt_id = str(request.get("attempt_id") or "")
        if not attempt_id:
            raise SchemaError("attempt_id is required")
        _verify_evaluation_binding(
            result_data=result_data,
            evaluation=evaluation,
            candidate_path=candidate_path,
            candidate_id=candidate_id,
            experiment_id=experiment_id,
            source_sha256=source_sha,
            attempt_id=attempt_id,
        )

        refs: dict[str, Any] = {}
        core_artifacts = {
            "candidate": (candidate_path, "candidate_record"),
            "evaluation": (result_path, "evaluation_record"),
            "sealed_manifest": (sealed_path, "sealed_manifest"),
            "baseline": (baseline_path, "baseline_measurement"),
            "working_source": (working_path, "working_source"),
        }
        if qualification_path:
            core_artifacts["qualification"] = (
                qualification_path, "qualification_record"
            )
        for name, (path, kind) in core_artifacts.items():
            refs[name] = self.store.register_artifact(
                path,
                root_id="input",
                kind=kind,
                owner_ref=candidate.ref_id,
                attempt_id=attempt_id,
                access=str(request.get("access", "public")),
                media_type="application/json" if path.suffix == ".json" else "text/plain",
            )
        for item in request.get("artifacts", []):
            if not isinstance(item, dict):
                raise SchemaError("artifacts must contain objects")
            name = str(item.get("name") or "")
            relative = item.get("path")
            if not name or not isinstance(relative, str):
                raise SchemaError("artifact name/path are required")
            if name in refs:
                raise SchemaError(f"duplicate artifact name: {name}")
            refs[name] = self.store.register_artifact(
                input_root / relative,
                root_id="input",
                kind=str(item.get("kind") or "raw_tool_output"),
                owner_ref=candidate.ref_id,
                attempt_id=attempt_id,
                access=str(item.get("access", request.get("access", "public"))),
                media_type=str(item.get("media_type", "text/plain")),
            )

        working_source_sha = _sha256_text(working_path.read_text())
        working_state = WorkingState.derive(
            working_source_sha256=working_source_sha, candidate=candidate
        )
        missing: list[dict[str, str]] = []
        baseline_bound = _verify_manifest_binding(
            role="baseline",
            artifact_path=baseline_path,
            request=request,
            sealed=sealed,
            missing=missing,
        )
        qualification_bound = _verify_manifest_binding(
            role="qualification",
            artifact_path=qualification_path,
            request=request,
            sealed=sealed,
            missing=missing,
        )
        raw_stage = str(evaluation.get("stage") or "unknown")
        normalized_stage = STAGE_MAP.get(raw_stage, "unknown")
        if normalized_stage == "unknown":
            missing.append({
                "field": "evaluation.normalized_stage",
                "reason": "not_supported",
                "detail": f"legacy stage {raw_stage!r} has no v1 mapping",
            })
        physical = (sealed.get("contract") or {}).get("physical") or {}
        tool_versions = (
            qualification.get("tool_versions") if qualification_bound else None
        )
        tool_fingerprint = (
            content_hash(tool_versions) if isinstance(tool_versions, dict) else None
        )
        if tool_fingerprint is None:
            missing.append({
                "field": "evaluation.tool_fingerprint",
                "reason": "not_collected",
                "detail": "qualification tool_versions are unavailable",
            })
        reference_fingerprint = content_hash(sealed.get("files", {}))
        conditions = {
            "part": physical.get("part"),
            "clock_period_ns": physical.get("clock_period_ns"),
            "tool_fingerprint": tool_fingerprint,
            "reference_fingerprint": reference_fingerprint,
        }
        checks, conflicts = _checks(evaluation, refs["evaluation"].ref_id)
        measurements = _measurements(
            evaluation,
            normalized_stage,
            refs["evaluation"].ref_id,
            conditions,
            missing,
        )
        extraction_records = _raw_extractions(
            store=self.store,
            refs=refs,
            stage=normalized_stage,
            clock_period_ns=conditions["clock_period_ns"],
        )
        metric_sources: dict[str, list[str]] = {}
        metric_conflicts: set[str] = set()
        if extraction_records:
            checks, measurements, metric_sources, metric_conflicts = (
                _reconcile_extractions(
                    checks=checks,
                    measurements=measurements,
                    records=extraction_records,
                    conditions=conditions,
                    conflicts=conflicts,
                    missing=missing,
                )
            )
        for check in checks:
            if not check.executed:
                missing.append({
                    "field": f"checks.{check.check_id}",
                    "reason": "not_run",
                    "detail": "legacy evidence does not establish execution",
                })
        artifact_refs = [ref.to_dict() for ref in refs.values()]
        manifest_payload = {
            "candidate_ref": candidate.to_dict(),
            "attempt_id": attempt_id,
            "raw_stage": raw_stage,
            "normalized_stage": normalized_stage,
            "contract_sha256": candidate.contract_sha256,
            "tool_fingerprint": tool_fingerprint,
            "reference_fingerprint": reference_fingerprint,
            "completion_status": str(evaluation.get("status") or "unknown"),
            "artifacts": artifact_refs,
            "parser_revision": PARSER_REVISION,
            "binding_status": {
                "candidate_evaluation": "verified",
                "baseline": "verified" if baseline_bound else "unverified",
                "qualification": "verified" if qualification_bound else "unverified",
            },
        }
        if extraction_records:
            manifest_payload["extraction_refs"] = [
                value["content_hash"] for value in extraction_records
            ]
        evaluation_manifest = self.store.write_record(
            "manifests", "chipcontext.evaluation-manifest.v1", manifest_payload
        )
        observations = []
        if evaluation.get("failure_class") or evaluation.get("raw_error"):
            observations.append({
                "kind": "failure_observation",
                "stage": normalized_stage,
                "failure_class": evaluation.get("failure_class"),
                "message": str(evaluation.get("raw_error") or "")[-8000:],
                "source_ref": refs["evaluation"].ref_id,
                "mapping_quality": "derived",
            })
        snapshot_payload = {
            "candidate_ref": candidate.to_dict(),
            "working_state": working_state.to_dict(),
            "evaluation_manifest_ref": evaluation_manifest["content_hash"],
            "checks": [check.to_dict() for check in checks],
            "measurements": [value.to_dict() for value in measurements],
            "observations": observations,
            "missing": missing,
            "conflicts": conflicts,
            "parser_revision": PARSER_REVISION,
            "source_refs": [ref.ref_id for ref in refs.values()],
            "completeness": {
                "status": "complete" if not missing and not conflicts else "partial",
                "missing_count": len(missing),
                "conflict_count": len(conflicts),
            },
        }
        if extraction_records:
            snapshot_payload["extraction_refs"] = [
                value["content_hash"] for value in extraction_records
            ]
            snapshot_payload["measurement_sources"] = metric_sources
        snapshot = self.store.write_record(
            "snapshots", "chipcontext.evidence-snapshot.v1", snapshot_payload
        )

        failure = bool(evaluation.get("failure_class")) or (
            evaluation.get("status") == "candidate_invalid"
        )
        if failure:
            raw_failure_kinds = (
                "differential_stderr",
                "differential_stdout",
                "elaboration_stderr",
                "elaboration_stdout",
                "raw_error",
            )
            raw_ref = next(
                (
                    ref for kind in raw_failure_kinds for ref in refs.values()
                    if ref.kind == kind
                ),
                refs["evaluation"],
            )
            differential = evaluation.get("differential")
            for field in ("expected", "actual"):
                if not isinstance(differential, dict) or differential.get(field) is None:
                    missing.append({
                        "field": f"failure.{field}",
                        "reason": "not_collected",
                        "detail": f"failure evidence has no structured {field} value",
                    })
            bundle_payload = {
                "kind": "failure",
                "recipe_revision": "failure_summary_v1",
                "question": "Why did this evaluated candidate fail its recorded gate?",
                "snapshot_refs": [snapshot["content_hash"]],
                "facts": {
                    "stage": normalized_stage,
                    "failure_class": evaluation.get("failure_class"),
                    "seed": candidate_data.get("seed"),
                    "expected": (
                        differential.get("expected")
                        if isinstance(differential, dict) else None
                    ),
                    "actual": (
                        differential.get("actual")
                        if isinstance(differential, dict) else None
                    ),
                    "diff_ref": refs["candidate"].ref_id,
                    "raw_error_ref": raw_ref.ref_id,
                },
                "conditions": conditions,
                "coverage": {
                    "checks": [check.to_dict() for check in checks],
                },
                "open_needs": [
                    item for item in missing
                    if item["field"].startswith(("checks.", "failure."))
                ],
                "drilldown_refs": [raw_ref.ref_id, refs["candidate"].ref_id],
                "source_refs": [refs["evaluation"].ref_id],
                "completeness": "partial" if missing or conflicts else "complete",
            }
        else:
            baseline_metrics = _metric_map(baseline)
            current_metrics = {
                value.metric_id: value for value in measurements
                if value.availability == "available"
                and value.metric_id not in metric_conflicts
            }
            baseline_stage_raw = baseline.get("stage")
            baseline_stage = (
                STAGE_MAP.get(baseline_stage_raw)
                if isinstance(baseline_stage_raw, str)
                else None
            )
            if baseline_stage_raw is None:
                missing.append({
                    "field": "baseline.stage",
                    "reason": "not_collected",
                    "detail": (
                        "bound baseline record does not identify its evaluation stage"
                    ),
                })
            elif baseline_stage is None:
                missing.append({
                    "field": "baseline.stage",
                    "reason": "not_supported",
                    "detail": (
                        f"baseline stage {baseline_stage_raw!r} has no v1 mapping"
                    ),
                })
            baseline_conditions = {
                "stage": baseline_stage,
                "part": baseline.get("part", conditions["part"])
                if baseline_bound else baseline.get("part"),
                "clock_period_ns": baseline.get(
                    "clock_period_ns",
                    conditions["clock_period_ns"] if baseline_bound else None,
                ),
                "tool_fingerprint": baseline.get(
                    "tool_fingerprint",
                    conditions["tool_fingerprint"]
                    if baseline_bound and qualification_bound else None,
                ),
                "reference_fingerprint": baseline.get(
                    "reference_fingerprint",
                    conditions["reference_fingerprint"] if baseline_bound else None,
                ),
            }
            current_comparison = {"stage": normalized_stage, **conditions}
            shared_metric_ids = sorted(set(baseline_metrics) & set(current_metrics))
            required_condition_keys = {
                "stage", "part", "clock_period_ns", "tool_fingerprint",
                "reference_fingerprint",
            }
            conditions_complete = all(
                baseline_conditions.get(key) is not None
                and current_comparison.get(key) is not None
                for key in required_condition_keys
            )
            comparable = (
                working_state.state == "evaluated_current"
                and baseline_bound
                and qualification_bound
                and baseline_conditions == current_comparison
                and bool(shared_metric_ids)
                and conditions_complete
            )
            deltas = []
            if comparable:
                for metric_id in shared_metric_ids:
                    current = current_metrics[metric_id]
                    base = _baseline_measurement(
                        baseline, metric_id, refs["baseline"].ref_id,
                        baseline_conditions,
                    )
                    if (
                        current.definition_revision != base.definition_revision
                        or current.unit != base.unit
                    ):
                        comparable = False
                        break
                    deltas.append({
                        "metric_id": metric_id,
                        "definition_revision": base.definition_revision,
                        "unit": base.unit,
                        "baseline": base.value,
                        "current": current.value,
                        "delta": current.value - base.value,
                    })
            if not comparable:
                deltas = []
                missing.append({
                    "field": "performance_delta",
                    "reason": "not_comparable",
                    "detail": (
                        "current working source has not been evaluated"
                        if working_state.state != "evaluated_current"
                        else (
                            "baseline and current evaluation share no supported metric"
                            if not shared_metric_ids
                            else (
                                "stage, part, clock, tool, reference, definition, "
                                "or unit is missing or differs"
                            )
                        )
                    ),
                })
            bundle_payload = {
                "kind": "performance_delta",
                "recipe_revision": "comparable_delta_v1",
                "question": "What changed under the same recorded implementation conditions?",
                "snapshot_refs": [snapshot["content_hash"]],
                "facts": {
                    "comparable": comparable,
                    "deltas": deltas,
                    "baseline_ref": refs["baseline"].ref_id,
                    "current_ref": refs["evaluation"].ref_id,
                },
                "conditions": {
                    "baseline": baseline_conditions,
                    "current": current_comparison,
                },
                "coverage": {
                    "metric_ids": sorted(set(baseline_metrics) | set(current_metrics)),
                },
                "open_needs": [
                    item for item in missing
                    if item["field"] in {"performance_delta", "baseline.stage"}
                ],
                "drilldown_refs": [
                    refs["baseline"].ref_id, refs["evaluation"].ref_id
                ],
                "source_refs": [
                    refs["baseline"].ref_id, refs["evaluation"].ref_id
                ],
                "completeness": (
                    "complete" if comparable and not conflicts else "partial"
                ),
            }
        bundle = self.store.write_record(
            "bundles", "chipcontext.evidence-bundle.v1", bundle_payload
        )

        bundle_scope = (
            "current"
            if working_state.state == "evaluated_current"
            else "last_evaluated_candidate"
        )
        selected = [
            {
                "kind": "candidate_identity",
                "summary": (
                    f"{candidate.experiment_id}/{candidate.candidate_id} "
                    f"source={candidate.source_sha256[:12]} "
                    f"contract={candidate.contract_sha256[:12]}"
                ),
            },
            {
                "kind": "working_state",
                "summary": (
                    "not_evaluated (last evaluation belongs to another revision)"
                    if working_state.state == "evaluated_other_revision"
                    else working_state.state
                ),
            },
            {
                "kind": "validation_status",
                "summary": ", ".join(
                    f"{check.check_id}={check.outcome if check.executed else 'not_run'}"
                    for check in checks
                ),
            },
            {
                "kind": "question_bundle",
                "summary": (
                    f"{bundle_payload['kind']} "
                    f"scope={bundle_scope} "
                    f"completeness={bundle_payload['completeness']}"
                ),
            },
        ]
        packet_payload = {
            "experiment_id": candidate.experiment_id,
            "candidate_ref": candidate.to_dict(),
            "working_state": working_state.to_dict(),
            "current_evaluation_status": (
                "not_evaluated"
                if working_state.state == "evaluated_other_revision"
                else working_state.state
            ),
            "snapshot_refs": [snapshot["content_hash"]],
            "bundle": {
                "ref": bundle["content_hash"],
                "kind": bundle_payload["kind"],
                "measurement_scope": bundle_scope,
            },
            "policy_revision": str(
                (request.get("policy") or {}).get("revision", POLICY_REVISION)
            ),
            "knowledge_acl": request.get("knowledge_acl") or {
                "target_specific": False
            },
            "selected": selected,
            "omitted": [],
            "missing": missing,
            "conflicts": conflicts,
            "drilldown_refs": [
                {"kind": ref.kind, "artifact_ref": ref.ref_id}
                for ref in refs.values()
            ],
            "completeness": {
                "status": "complete" if not missing and not conflicts else "partial",
                "missing_count": len(missing),
                "conflict_count": len(conflicts),
            },
        }
        max_bytes = int((request.get("policy") or {}).get("max_payload_bytes", 16384))
        packet_payload, required_bytes = _apply_packet_budget(
            packet_payload, max_bytes
        )
        packet = self.store.write_record(
            "packets", "chipcontext.context-packet.v1", packet_payload
        )
        if len(canonical_json(packet).encode()) != required_bytes:
            raise RuntimeError("context packet byte accounting changed during publish")
        markdown = _render_markdown(packet)
        self.store.publish_alias("evaluation-manifest.json", evaluation_manifest)
        self.store.publish_alias("snapshot.json", snapshot)
        self.store.publish_alias("bundle.json", bundle)
        self.store.publish_alias("packet.json", packet)
        self.store.publish_alias("context.md", markdown)
        self.store.append_event({
            "event": "evidence_prepared",
            "candidate_ref": candidate.ref_id,
            "input_hashes": {
                name: ref.sha256 for name, ref in refs.items()
            },
            "outputs": {
                "manifest": evaluation_manifest["content_hash"],
                "snapshot": snapshot["content_hash"],
                "bundle": bundle["content_hash"],
                "packet": packet["content_hash"],
            },
            "active_seconds": time.monotonic() - started,
        })
        return PreparationResult(
            evaluation_manifest=evaluation_manifest,
            snapshot=snapshot,
            bundle=bundle,
            packet=packet,
            markdown=markdown,
        )
