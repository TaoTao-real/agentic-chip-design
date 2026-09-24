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
    require_sha256,
)
from .store import EvidenceStore


PARSER_REVISION = "legacy-evaluation-v1"
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
            "packet_bytes": len(self.markdown.encode()),
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
    if not rows or "success" not in rows[-1]:
        return None
    return bool(rows[-1]["success"])


def _checks(
    evaluation: dict[str, Any], source_ref: str,
) -> tuple[list[CheckRecord], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    differential = evaluation.get("differential")
    build_executed = bool(evaluation.get("build_ok")) or _stage_executed(
        evaluation, {"elaboration"}
    )
    correctness_executed = isinstance(differential, dict) or _stage_executed(
        evaluation, {"correctness"}
    )
    interface_executed = (
        isinstance(differential, dict) and "interface_ok" in differential
    ) or (
        correctness_executed and "interface_ok" in evaluation
    )
    synth_executed = evaluation.get("post_synth") is not None or _stage_executed(
        evaluation, {"synthesis", "post_synth"}
    )
    route_executed = evaluation.get("post_route") is not None or _stage_executed(
        evaluation, {"route", "post_route"}
    )
    regression_executed = evaluation.get("regression") is not None or _stage_executed(
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

    regression = evaluation.get("regression")
    regression_passed = None
    if isinstance(regression, dict):
        regression_passed = bool(regression.get("passed"))
    build_passed = bool(evaluation.get("build_ok")) if build_executed else None
    if build_executed and not evaluation.get("build_ok"):
        build_passed = _stage_outcome(evaluation, {"elaboration"})
    correctness_passed = None
    if correctness_executed:
        if "correctness_ok" in evaluation:
            correctness_passed = bool(evaluation["correctness_ok"])
        elif isinstance(differential, dict) and "passed" in differential:
            correctness_passed = bool(differential["passed"])
        else:
            correctness_passed = _stage_outcome(evaluation, {"correctness"})
    synth_passed = _stage_outcome(evaluation, {"synthesis", "post_synth"})
    if synth_passed is None and synth_executed:
        synth_passed = isinstance(evaluation.get("post_synth"), dict)
    route_passed = _stage_outcome(evaluation, {"route", "post_route"})
    if route_passed is None and route_executed:
        route_passed = isinstance(evaluation.get("post_route"), dict)

    def reconcile(
        check_id: str,
        explicit: bool | None,
        staged: bool | None,
        current: bool | None,
    ) -> bool | None:
        if explicit is not None and staged is not None and explicit != staged:
            conflicts.append({
                "field": f"checks.{check_id}",
                "reason": "conflicting_sources",
                "detail": (
                    f"legacy boolean={str(explicit).lower()} conflicts with "
                    f"stage outcome={str(staged).lower()}"
                ),
                "source_ref": source_ref,
            })
            return None
        return current

    build_passed = reconcile(
        "elaboration",
        bool(evaluation["build_ok"]) if evaluation.get("build_ok") is True else None,
        _stage_outcome(evaluation, {"elaboration"}),
        build_passed,
    )
    correctness_passed = reconcile(
        "differential_correctness",
        bool(evaluation["correctness_ok"])
        if "correctness_ok" in evaluation else None,
        _stage_outcome(evaluation, {"correctness"}),
        correctness_passed,
    )
    checks = [
        record("elaboration", build_executed, build_passed),
        # A legacy lint_ok=False does not prove lint was executed.
        record("lint", _stage_executed(evaluation, {"lint"}), None),
        record("interface_signature", interface_executed, (
            bool(
                differential.get("interface_ok")
                if isinstance(differential, dict) and "interface_ok" in differential
                else evaluation.get("interface_ok")
            ) if interface_executed else None
        )),
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
            return input_root / raw

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
        evaluated_id = str(evaluation.get("candidate_id") or "")
        if evaluated_id and evaluated_id != candidate_id:
            raise SchemaError("evaluation candidate ID does not match candidate record")
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
        raw_stage = str(evaluation.get("stage") or "unknown")
        normalized_stage = STAGE_MAP.get(raw_stage, "unknown")
        missing: list[dict[str, str]] = []
        if normalized_stage == "unknown":
            missing.append({
                "field": "evaluation.normalized_stage",
                "reason": "not_supported",
                "detail": f"legacy stage {raw_stage!r} has no v1 mapping",
            })
        physical = (sealed.get("contract") or {}).get("physical") or {}
        tool_versions = qualification.get("tool_versions")
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
        condition_overrides = request.get("current_conditions") or {}
        if not isinstance(condition_overrides, dict):
            raise SchemaError("current_conditions must be an object")
        conditions.update(condition_overrides)
        checks, conflicts = _checks(evaluation, refs["evaluation"].ref_id)
        measurements = _measurements(
            evaluation,
            normalized_stage,
            refs["evaluation"].ref_id,
            conditions,
            missing,
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
        }
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
            }
            baseline_conditions = {
                "stage": str(baseline.get("stage", normalized_stage)),
                "part": baseline.get("part", conditions["part"]),
                "clock_period_ns": baseline.get(
                    "clock_period_ns", conditions["clock_period_ns"]
                ),
                "tool_fingerprint": baseline.get(
                    "tool_fingerprint", conditions["tool_fingerprint"]
                ),
                "reference_fingerprint": baseline.get(
                    "reference_fingerprint", conditions["reference_fingerprint"]
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
                    if item["field"] == "performance_delta"
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
                    "scope="
                    f"{'current' if working_state.state == 'evaluated_current' else 'last_evaluated_candidate'} "
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
                "measurement_scope": (
                    "current"
                    if working_state.state == "evaluated_current"
                    else "last_evaluated_candidate"
                ),
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
        required_bytes = len(canonical_json(packet_payload).encode())
        if required_bytes > max_bytes:
            raise SchemaError(
                "required_evidence_overflow: "
                f"required={required_bytes} max={max_bytes}"
            )
        packet_payload["budget"] = {
            "unit": "serialized_bytes",
            "maximum": max_bytes,
            "required": required_bytes,
            "overflow_policy": "fail_closed",
        }
        packet = self.store.write_record(
            "packets", "chipcontext.context-packet.v1", packet_payload
        )
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
