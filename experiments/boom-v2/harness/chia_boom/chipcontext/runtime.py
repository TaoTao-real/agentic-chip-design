from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..artifacts import CandidateArtifact, EvaluationArtifact, dump_json, sha256_text
from .query import (
    ChipContextQueryService,
    EvidenceHandle,
    QueryScope,
)
from .schema import CandidateRef, SchemaError, content_hash
from .service import ChipContextService
from .store import EvidenceMeter, EvidenceStore


FEEDBACK_ARMS = ("E0", "E1")
RUNTIME_CONTEXT_SCHEMA = "chipcontext.runtime-context.v1"
STRUCTURED_FEEDBACK_SCHEMA = "chipcontext.runtime-feedback.v1"


@dataclass(frozen=True)
class RuntimeContext:
    store_id: str
    store_root: str
    snapshot_ref: str
    candidate_ref: dict[str, Any]
    attempt_id: str
    packet_ref: str
    bundle_ref: str
    extraction_refs: tuple[str, ...]
    prepare_cost: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": RUNTIME_CONTEXT_SCHEMA,
            "store_id": self.store_id,
            "store_root": self.store_root,
            "snapshot_ref": self.snapshot_ref,
            "candidate_ref": self.candidate_ref,
            "attempt_id": self.attempt_id,
            "packet_ref": self.packet_ref,
            "bundle_ref": self.bundle_ref,
            "extraction_refs": list(self.extraction_refs),
            "prepare_cost": self.prepare_cost,
        }
        return payload | {"context_hash": content_hash(payload)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RuntimeContext":
        if value.get("schema_version") != RUNTIME_CONTEXT_SCHEMA:
            raise SchemaError("unsupported runtime context schema")
        unsigned = {key: item for key, item in value.items() if key != "context_hash"}
        if value.get("context_hash") != content_hash(unsigned):
            raise SchemaError("runtime context failed its content identity")
        return cls(
            store_id=str(value["store_id"]),
            store_root=str(value["store_root"]),
            snapshot_ref=str(value["snapshot_ref"]),
            candidate_ref=dict(value["candidate_ref"]),
            attempt_id=str(value["attempt_id"]),
            packet_ref=str(value["packet_ref"]),
            bundle_ref=str(value["bundle_ref"]),
            extraction_refs=tuple(value.get("extraction_refs", [])),
            prepare_cost=dict(value.get("prepare_cost", {})),
        )


def _write_exact(path: Path, value: Any) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text() != rendered:
            raise RuntimeError(f"runtime evidence input changed: {path.name}")
        return
    dump_json(path, value)


def _relative(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise SchemaError("runtime evidence escaped the campaign root") from exc


def _raw_artifacts(
    candidate_dir: Path,
    campaign_root: Path,
    infrastructure_attempts: int,
) -> list[dict[str, Any]]:
    attempt_dir = candidate_dir / f"evaluation-attempt-{infrastructure_attempts:02d}"
    evidence_dir = attempt_dir if attempt_dir.is_dir() else candidate_dir
    candidates = [
        ("elaboration_stdout", "elaboration_stdout", evidence_dir / "elaboration/elaboration.stdout", "text/plain"),
        ("elaboration_stderr", "elaboration_stderr", evidence_dir / "elaboration/elaboration.stderr", "text/plain"),
        ("candidate_diff", "candidate_diff", evidence_dir / "elaboration/applied.diff", "text/plain"),
        ("differential_stdout", "differential_stdout", evidence_dir / "differential/driver.stdout", "text/plain"),
        ("differential_stderr", "differential_stderr", evidence_dir / "differential/driver.stderr", "text/plain"),
        ("differential_result", "differential_result", evidence_dir / "differential/run/result.json", "application/json"),
        ("vivado_stdout", "vivado_stdout", evidence_dir / "vivado/vivado.stdout", "text/plain"),
        ("vivado_stderr", "vivado_stderr", evidence_dir / "vivado/vivado.stderr", "text/plain"),
    ]
    summary = evidence_dir / "vivado/post_synth_timing_summary.rpt"
    utilization = evidence_dir / "vivado/post_synth_utilization.rpt"
    if summary.is_file() and utilization.is_file():
        candidates.extend([
            ("post_synth_timing_summary", "post_synth_timing_summary", summary, "text/plain"),
            ("post_synth_utilization", "post_synth_utilization", utilization, "text/plain"),
        ])
        paths = evidence_dir / "vivado/post_synth_timing_paths.rpt"
        if paths.is_file():
            candidates.append((
                "post_synth_timing_paths", "post_synth_timing_paths", paths,
                "text/plain",
            ))
    else:
        for name, path in (
            ("vivado_partial_timing", summary),
            ("vivado_partial_utilization", utilization),
        ):
            if path.is_file():
                candidates.append((name, "vivado_diagnostic", path, "text/plain"))
    return [
        {
            "name": name,
            "kind": kind,
            "path": _relative(campaign_root, path),
            "access": "controlled",
            "media_type": media_type,
        }
        for name, kind, path, media_type in candidates
        if path.is_file()
    ]


def prepare_runtime_context(
    *,
    campaign_root: Path,
    candidate_dir: Path,
    candidate: CandidateArtifact,
    evaluation: EvaluationArtifact,
    target_id: str,
    infrastructure_attempts: int,
) -> RuntimeContext:
    """Seal one completed evaluation for the next Agent turn.

    The evaluation and raw tool files remain in their original campaign
    directory.  ChipContext stores only content-addressed records and controlled
    references to those bytes.
    """

    campaign_root = campaign_root.resolve(strict=True)
    candidate_dir = candidate_dir.resolve(strict=True)
    candidate_dir.relative_to(campaign_root)
    if evaluation.candidate_id != candidate.id:
        raise SchemaError("evaluation belongs to another candidate")
    descriptor_path = candidate_dir / "chipcontext-context.json"
    attempt_id = f"{candidate.id}-evaluation-{infrastructure_attempts:02d}"
    if descriptor_path.is_file():
        context = RuntimeContext.from_dict(json.loads(descriptor_path.read_text()))
        if (
            context.candidate_ref.get("candidate_id") != candidate.id
            or context.candidate_ref.get("source_sha256") != sha256_text(candidate.source)
            or context.attempt_id != attempt_id
        ):
            raise RuntimeError("saved runtime context belongs to another evaluation")
        # Opening read-only verifies the store registry still exists and is not
        # silently recreated during resume.
        EvidenceStore(Path(context.store_root), read_only=True)
        return context

    result_path = candidate_dir / "result.json"
    if not result_path.is_file():
        raise FileNotFoundError("candidate result must be saved before preparation")
    prepared_result_path = candidate_dir / "chipcontext-evaluation.json"
    prepared_result = json.loads(result_path.read_text())
    prepared_result["attempt_id"] = attempt_id
    _write_exact(prepared_result_path, prepared_result)
    working_source = candidate_dir / "working-source.scala"
    if working_source.exists():
        if working_source.read_text() != candidate.source:
            raise RuntimeError("saved working source differs from evaluated candidate")
    else:
        working_source.write_text(candidate.source)

    frozen = campaign_root / "frozen"
    sealed = frozen / "FROZEN_RUN_MANIFEST.json"
    baseline = frozen / "targets" / target_id / "baseline-ppa.json"
    qualification = frozen / "qualification" / "QUALIFICATION.json"
    for required in (sealed, baseline, qualification, candidate_dir / "candidate.json"):
        if not required.is_file():
            raise FileNotFoundError(f"runtime evidence input is missing: {required.name}")

    request_path = candidate_dir / "chipcontext-request.json"
    request = {
        "schema_version": "chipcontext.prepare-request.v1",
        "experiment_id": candidate.campaign_id,
        "attempt_id": attempt_id,
        "input_root": os.path.relpath(campaign_root, candidate_dir),
        "candidate": _relative(campaign_root, candidate_dir / "candidate.json"),
        "evaluation": _relative(campaign_root, prepared_result_path),
        "sealed_manifest": _relative(campaign_root, sealed),
        "baseline": _relative(campaign_root, baseline),
        "qualification": _relative(campaign_root, qualification),
        "working_source": _relative(campaign_root, working_source),
        "manifest_bindings": {
            "baseline": f"targets/{target_id}/baseline-ppa.json",
            "qualification": "qualification/QUALIFICATION.json",
        },
        "access": "controlled",
        "knowledge_acl": {"target_specific": False},
        "policy": {
            "max_payload_bytes": 16 * 1024,
            "revision": "cc03-runtime-feedback-v1",
        },
        "artifacts": _raw_artifacts(
            candidate_dir,
            campaign_root,
            infrastructure_attempts,
        ),
    }
    _write_exact(request_path, request)
    store_root = candidate_dir / "chipcontext-store"
    meter = EvidenceMeter()
    started = time.monotonic_ns()
    store = EvidenceStore(store_root, {"input": campaign_root}, meter=meter)
    prepared = ChipContextService(store).prepare(request_path)
    elapsed = time.monotonic_ns() - started
    candidate_ref = dict(prepared.snapshot["candidate_ref"])
    context = RuntimeContext(
        store_id=candidate.id,
        store_root=str(store_root),
        snapshot_ref=prepared.snapshot["content_hash"],
        candidate_ref=candidate_ref,
        attempt_id=attempt_id,
        packet_ref=prepared.packet["content_hash"],
        bundle_ref=prepared.bundle["content_hash"],
        extraction_refs=tuple(prepared.snapshot.get("extraction_refs", [])),
        prepare_cost={
            "wall_time_ns": elapsed,
            **meter.snapshot(),
            "model_calls": 0,
            "eda_calls": 0,
        },
    )
    _write_exact(descriptor_path, context.to_dict())
    return context


def _scope(context: RuntimeContext, working_source: str) -> QueryScope:
    candidate = CandidateRef(**{
        key: value for key, value in context.candidate_ref.items()
        if key != "ref_id"
    })
    return QueryScope(
        EvidenceHandle(context.store_id, context.snapshot_ref),
        expected_candidate=candidate,
        expected_attempt_id=context.attempt_id,
        working_source_sha256=sha256_text(working_source),
    )


def query_runtime_context(
    context: RuntimeContext,
    *,
    working_source: str,
    operation: str,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one allow-listed read-only domain query with logical cost data."""

    parameters = dict(parameters or {})
    meter = EvidenceMeter()
    store = EvidenceStore(Path(context.store_root), read_only=True, meter=meter)
    service = ChipContextQueryService(
        {context.store_id: store},
        {context.store_id: {"public", "controlled"}},
    )
    scope = _scope(context, working_source)
    started = time.monotonic_ns()
    if operation == "candidate_status":
        if parameters:
            raise SchemaError("candidate_status accepts no parameters")
        answer = service.get_candidate_status(scope)
    elif operation == "candidate_artifacts":
        allowed = {"stage", "kinds", "limit", "cursor"}
        if set(parameters) - allowed:
            raise SchemaError("candidate_artifacts received unknown parameters")
        answer = service.list_candidate_artifacts(scope, **parameters)
    elif operation == "failure":
        allowed = {"check", "extraction_ref"}
        if set(parameters) - allowed:
            raise SchemaError("failure received unknown parameters")
        answer = service.get_failure_bundle(scope, **parameters)
    elif operation == "compare_metrics":
        allowed = {"reference", "stage", "metric_ids"}
        if set(parameters) - allowed:
            raise SchemaError("compare_metrics received unknown parameters")
        answer = service.compare_metrics(scope, **parameters)
    elif operation == "timing_paths":
        allowed = {
            "extraction_ref", "stage", "path_group", "source",
            "destination", "limit", "cursor",
        }
        if set(parameters) - allowed:
            raise SchemaError("timing_paths received unknown parameters")
        answer = service.query_timing_paths(scope, **parameters)
    elif operation == "read_artifact":
        allowed = {
            "artifact_ref", "start_line", "line_count", "cursor", "limit_bytes",
        }
        if set(parameters) - allowed:
            raise SchemaError("read_artifact received unknown parameters")
        answer = service.read_artifact(scope, **parameters)
    else:
        raise SchemaError("unsupported runtime query operation")
    elapsed = time.monotonic_ns() - started
    return {
        "answer": answer,
        "cost": {
            "wall_time_ns": elapsed,
            **meter.snapshot(),
            "cache_status": "not_configured",
            "physical_io_bytes": None,
            "peak_memory_bytes": None,
        },
    }


def _vivado_extraction(context: RuntimeContext) -> str | None:
    store = EvidenceStore(Path(context.store_root), read_only=True)
    for ref in context.extraction_refs:
        record = store.read_record("extractions", ref)
        if record.get("extractor", {}).get("name") == "vivado":
            return ref
    return None


def _failed_check(status: dict[str, Any]) -> str | None:
    checks = status.get("answer", {}).get("result", {}).get("checks", [])
    for check in checks:
        if check.get("executed") and check.get("outcome") in {"fail", "inconclusive"}:
            return str(check.get("check_id"))
    return None


def _tagged(values: Iterable[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    return [{"query": query, **value} for value in values]


def _compact_metric_result(value: dict[str, Any]) -> dict[str, Any]:
    comparisons = []
    for row in value.get("comparisons", []):
        compact = {
            "metric_id": row.get("metric_id"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "delta": row.get("delta"),
        }
        for side in ("reference", "current"):
            measurement = row.get(side)
            compact[side] = None if not isinstance(measurement, dict) else {
                "value": measurement.get("value"),
                "unit": measurement.get("unit"),
                "stage": (measurement.get("scope") or {}).get("stage"),
                "source_ref": measurement.get("source_ref"),
                "availability": measurement.get("availability"),
            }
        comparisons.append(compact)
    return {
        "all_comparable": value.get("all_comparable"),
        "any_comparable": value.get("any_comparable"),
        "comparisons": comparisons,
    }


def _compact_timing_result(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {"availability": "not_collected", "paths": []}

    def compact_fact(fact: Any) -> Any:
        if not isinstance(fact, dict):
            return fact
        return {
            key: fact.get(key)
            for key in (
                "availability", "rank", "source", "destination", "path_group",
                "slack_ns", "requirement_ns", "data_path_delay_ns", "logic_levels",
                "status", "source_location",
            )
            if key in fact
        }

    report_first = value.get("report_first") or {}
    filtered = value.get("filtered_minimum") or {}
    global_worst = value.get("global_worst") or {}
    return {
        "report_first": {
            "availability": report_first.get("availability"),
            "fact": compact_fact(report_first.get("fact")),
        },
        "filtered_minimum": {
            "availability": filtered.get("availability"),
            "scope": filtered.get("scope"),
            "definite_match_count": filtered.get("definite_match_count"),
            "possible_match_count": filtered.get("possible_match_count"),
            "fact": compact_fact(filtered.get("fact")),
        },
        "global_worst": {
            "availability": global_worst.get("availability"),
            "reason": global_worst.get("reason"),
        },
        "paths": [compact_fact(row) for row in value.get("paths", [])[:3]],
    }


def _compact_source_refs(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for value in values:
        # Artifact refs already appear in checks, metric sides, timing locations,
        # and the raw-artifact inventory. Keep only the higher-level extraction
        # and bounded source-location entries in the one-screen push.
        if value.get("type") not in {"extraction", "source_location"}:
            continue
        key = (
            str(value.get("query", "")),
            str(value.get("type", "")),
            str(value.get("ref", "")),
        )
        if not key[2] or key in seen:
            continue
        seen.add(key)
        row = {name: value.get(name) for name in ("query", "type", "ref")}
        if value.get("location") is not None:
            row["location"] = value["location"]
        compact.append(row)
    return compact


def build_structured_feedback(
    context: RuntimeContext,
    *,
    working_source: str,
) -> dict[str, Any]:
    """Build the one-screen E1 push from deterministic domain queries."""

    queries: dict[str, dict[str, Any]] = {}
    status = query_runtime_context(
        context, working_source=working_source, operation="candidate_status"
    )
    queries["candidate_status"] = status
    metrics = query_runtime_context(
        context,
        working_source=working_source,
        operation="compare_metrics",
        parameters={
            "reference": "bound_baseline",
            "stage": "post_synth",
            "metric_ids": ["critical_delay_ns", "slice_luts"],
        },
    )
    queries["compare_metrics"] = metrics
    artifacts = query_runtime_context(
        context,
        working_source=working_source,
        operation="candidate_artifacts",
        parameters={"limit": 100},
    )
    queries["candidate_artifacts"] = artifacts
    failed = _failed_check(status)
    failure = None
    if failed is not None:
        failure = query_runtime_context(
            context,
            working_source=working_source,
            operation="failure",
            parameters={"check": failed},
        )
        queries["failure"] = failure
    timing = None
    timing_ref = _vivado_extraction(context)
    if timing_ref is not None:
        timing = query_runtime_context(
            context,
            working_source=working_source,
            operation="timing_paths",
            parameters={
                "extraction_ref": timing_ref,
                "stage": "post_synth",
                "limit": 3,
            },
        )
        queries["timing_paths"] = timing

    answers = {name: value["answer"] for name, value in queries.items()}
    missing: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    for name, answer in answers.items():
        missing.extend(_tagged(answer.get("missing", []), name))
        conflicts.extend(_tagged(answer.get("conflicts", []), name))
        source_refs.extend(_tagged(answer.get("source_refs", []), name))
    status_result = status["answer"]["result"]
    result = {
        "schema_version": STRUCTURED_FEEDBACK_SCHEMA,
        "candidate": {
            "candidate_id": context.candidate_ref["candidate_id"],
            "source_sha256": context.candidate_ref["source_sha256"],
            "attempt_id": context.attempt_id,
            "snapshot_ref": context.snapshot_ref,
        },
        "applicability": status["answer"]["applicability"],
        "status": {
            "completion_status": status_result.get("completion_status"),
            "raw_stage": status_result.get("raw_stage"),
            "normalized_stage": status_result.get("normalized_stage"),
            "final_chip_acceptance": status_result.get("final_chip_acceptance"),
            "checks": [
                {
                    "check_id": row.get("check_id"),
                    "executed": row.get("executed"),
                    "outcome": row.get("outcome"),
                    "source_ref": row.get("source_ref"),
                }
                for row in status_result.get("checks", [])
            ],
        },
        "failure": failure["answer"]["result"] if failure else None,
        "metrics": _compact_metric_result(metrics["answer"]["result"]),
        "timing_paths": _compact_timing_result(
            timing["answer"]["result"] if timing else None
        ),
        "raw_artifacts": [
            {
                key: row.get(key)
                for key in ("kind", "stage", "content_ref", "media_type", "size_bytes")
            }
            for row in artifacts["answer"]["result"]["artifacts"]
        ],
        "missing": missing,
        "conflicts": conflicts,
        "source_refs": _compact_source_refs(source_refs),
    }
    result["content_hash"] = content_hash(result)
    # Dynamic cost is intentionally outside the deterministic content identity
    # and is retained in the campaign audit rather than sent to the Agent.
    result["cost"] = {
        "prepare": context.prepare_cost,
        "queries": {name: value["cost"] for name, value in queries.items()},
    }
    return result
