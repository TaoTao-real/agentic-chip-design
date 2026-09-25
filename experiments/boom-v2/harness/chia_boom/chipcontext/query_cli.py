from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .query import (
    ChipContextQueryService,
    EvidenceHandle,
    QueryError,
    QueryScope,
)
from .render import render_json, render_markdown
from .schema import ACCESS_LEVELS, CandidateRef, SchemaError
from .store import EvidenceMeter, EvidenceStore


QUERY_REQUEST_SCHEMA = "chipcontext.query-request.v1"
QUERY_RESPONSE_SCHEMA = "chipcontext.query-response.v1"
QUERY_COST_SCHEMA = "chipcontext.query-cost.v1"
STORE_REGISTRY_SCHEMA = "chipcontext.store-registry.v1"
DEFAULT_OUTPUT_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
_STORE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class ParsedQuery:
    operation: str
    scope: QueryScope
    parameters: dict[str, Any]
    referenced_store_ids: frozenset[str]


@dataclass
class QueryExecution:
    answer: dict[str, Any]
    meter: EvidenceMeter
    started_ns: int


def _strict_object(
    value: Any,
    *,
    name: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be an object")
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise SchemaError(f"{name} contains unknown fields")
    if missing:
        raise SchemaError(f"{name} is missing required fields")
    return value


def _read_json_object(path: Path, meter: EvidenceMeter, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SchemaError(f"{name} is unavailable")
    data = path.read_bytes()
    meter.configuration_read_count += 1
    meter.configuration_read_bytes += len(data)
    meter.parse_bytes += len(data)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"{name} must be a JSON object")
    return value


def _parse_candidate(value: Any) -> CandidateRef | None:
    if value is None:
        return None
    raw = _strict_object(
        value,
        name="expected_candidate",
        required={
            "experiment_id",
            "candidate_id",
            "source_sha256",
            "contract_sha256",
        },
        optional={"parent_ref", "ref_id"},
    )
    for field in (
        "experiment_id",
        "candidate_id",
        "source_sha256",
        "contract_sha256",
    ):
        if not isinstance(raw[field], str) or not raw[field]:
            raise SchemaError(f"expected_candidate.{field} must be a string")
    parent_ref = raw.get("parent_ref")
    if parent_ref is not None:
        parent_ref = _strict_object(
            parent_ref,
            name="expected_candidate.parent_ref",
            required={"candidate_id", "source_sha256"},
        )
        if (
            not isinstance(parent_ref["candidate_id"], str)
            or not parent_ref["candidate_id"]
            or not isinstance(parent_ref["source_sha256"], str)
        ):
            raise SchemaError(
                "expected_candidate.parent_ref fields must be strings"
            )
    candidate = CandidateRef(
        experiment_id=raw["experiment_id"],
        candidate_id=raw["candidate_id"],
        source_sha256=raw["source_sha256"],
        contract_sha256=raw["contract_sha256"],
        parent_ref=parent_ref,
    )
    if raw.get("ref_id") is not None and raw["ref_id"] != candidate.ref_id:
        raise SchemaError("expected_candidate ref_id is inconsistent")
    return candidate


def _parse_scope(value: Any, name: str = "scope") -> QueryScope:
    raw = _strict_object(
        value,
        name=name,
        required={"handle"},
        optional={
            "expected_candidate",
            "expected_attempt_id",
            "working_source_sha256",
        },
    )
    handle = _strict_object(
        raw["handle"],
        name=f"{name}.handle",
        required={"store_id", "snapshot_ref"},
    )
    expected_attempt = raw.get("expected_attempt_id")
    if expected_attempt is not None and (
        not isinstance(expected_attempt, str) or not expected_attempt
    ):
        raise SchemaError(f"{name}.expected_attempt_id must be a string or null")
    working_source = raw.get("working_source_sha256")
    if working_source is not None and not isinstance(working_source, str):
        raise SchemaError(
            f"{name}.working_source_sha256 must be a string or null"
        )
    return QueryScope(
        EvidenceHandle(handle["store_id"], handle["snapshot_ref"]),
        expected_candidate=_parse_candidate(raw.get("expected_candidate")),
        expected_attempt_id=expected_attempt,
        working_source_sha256=working_source,
    )


_PARAMETERS: dict[str, tuple[set[str], set[str]]] = {
    "candidate_status": (set(), set()),
    "candidate_artifacts": (set(), {"stage", "kinds", "limit", "cursor"}),
    "failure": ({"check"}, {"extraction_ref"}),
    "compare_metrics": ({"reference", "stage", "metric_ids"}, set()),
    "timing_paths": (
        {"extraction_ref", "stage"},
        {"path_group", "source", "destination", "limit", "cursor"},
    ),
    "read_artifact": (
        {"artifact_ref"},
        {"start_line", "line_count", "cursor", "limit_bytes"},
    ),
}


def _validate_parameter_types(operation: str, parameters: dict[str, Any]) -> None:
    def optional_string(name: str) -> None:
        value = parameters.get(name)
        if value is not None and not isinstance(value, str):
            raise SchemaError(f"{operation}.{name} must be a string or null")

    def optional_integer(name: str) -> None:
        value = parameters.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise SchemaError(f"{operation}.{name} must be an integer or null")

    if operation == "candidate_artifacts":
        optional_string("stage")
        optional_string("cursor")
        optional_integer("limit")
        kinds = parameters.get("kinds")
        if kinds is not None and (
            not isinstance(kinds, list)
            or any(not isinstance(value, str) for value in kinds)
        ):
            raise SchemaError("candidate_artifacts.kinds must be an array of strings")
    elif operation == "failure":
        if not isinstance(parameters["check"], str):
            raise SchemaError("failure.check must be a string")
        optional_string("extraction_ref")
    elif operation == "compare_metrics":
        if not isinstance(parameters["stage"], str):
            raise SchemaError("compare_metrics.stage must be a string")
        metric_ids = parameters["metric_ids"]
        if (
            not isinstance(metric_ids, list)
            or any(not isinstance(value, str) for value in metric_ids)
        ):
            raise SchemaError("compare_metrics.metric_ids must be an array of strings")
    elif operation == "timing_paths":
        if not isinstance(parameters["extraction_ref"], str):
            raise SchemaError("timing_paths.extraction_ref must be a string")
        if not isinstance(parameters["stage"], str):
            raise SchemaError("timing_paths.stage must be a string")
        for name in ("path_group", "source", "destination", "cursor"):
            optional_string(name)
        optional_integer("limit")
    elif operation == "read_artifact":
        if not isinstance(parameters["artifact_ref"], str):
            raise SchemaError("read_artifact.artifact_ref must be a string")
        optional_string("cursor")
        for name in ("start_line", "line_count", "limit_bytes"):
            optional_integer(name)


def parse_query_request(value: Any) -> ParsedQuery:
    raw = _strict_object(
        value,
        name="query request",
        required={"schema_version", "operation", "scope", "parameters"},
    )
    if raw["schema_version"] != QUERY_REQUEST_SCHEMA:
        raise SchemaError("query request schema is unsupported")
    operation = raw["operation"]
    if not isinstance(operation, str) or operation not in _PARAMETERS:
        raise SchemaError("query operation is unsupported")
    if not isinstance(raw["parameters"], dict):
        raise SchemaError("query parameters must be an object")
    required, optional = _PARAMETERS[operation]
    parameters = _strict_object(
        raw["parameters"],
        name=f"{operation} parameters",
        required=required,
        optional=optional,
    )
    _validate_parameter_types(operation, parameters)
    scope = _parse_scope(raw["scope"])
    store_ids = {scope.handle.store_id}
    normalized = dict(parameters)
    if operation == "compare_metrics":
        reference = parameters["reference"]
        if reference == "bound_baseline":
            normalized["reference"] = reference
        else:
            reference_scope = _parse_scope(reference, "reference scope")
            normalized["reference"] = reference_scope
            store_ids.add(reference_scope.handle.store_id)
    return ParsedQuery(
        operation=operation,
        scope=scope,
        parameters=normalized,
        referenced_store_ids=frozenset(store_ids),
    )


def _resolve_store_root(registry_path: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise SchemaError("store root must be a non-empty relative path")
    cursor = registry_path.parent.resolve(strict=True)
    for component in Path(value).parts:
        if component in {"", "."}:
            continue
        if component == "..":
            cursor = cursor.parent
            continue
        cursor = cursor / component
        if cursor.is_symlink():
            raise SchemaError("store root may not traverse a symlink")
    resolved = cursor.resolve(strict=True)
    if not resolved.is_dir():
        raise SchemaError("store root is not a directory")
    return resolved


def load_stores(
    registry_path: Path,
    registry_value: Any,
    *,
    required_store_ids: frozenset[str],
    allow_controlled: bool,
    meter: EvidenceMeter,
) -> tuple[dict[str, EvidenceStore], dict[str, set[str]]]:
    raw = _strict_object(
        registry_value,
        name="store registry",
        required={"schema_version", "stores"},
    )
    if raw["schema_version"] != STORE_REGISTRY_SCHEMA:
        raise SchemaError("store registry schema is unsupported")
    if not isinstance(raw["stores"], list) or not raw["stores"]:
        raise SchemaError("store registry requires a non-empty stores list")
    entries: dict[str, dict[str, Any]] = {}
    for value in raw["stores"]:
        entry = _strict_object(
            value,
            name="store registry entry",
            required={"store_id", "root", "allowed_access"},
        )
        store_id = entry["store_id"]
        if not isinstance(store_id, str) or not _STORE_ID.fullmatch(store_id):
            raise SchemaError("store_id must be a normalized logical identifier")
        if store_id in entries:
            raise SchemaError("store registry contains a duplicate store_id")
        levels = entry["allowed_access"]
        if (
            not isinstance(levels, list)
            or not levels
            or any(not isinstance(level, str) for level in levels)
            or len(set(levels)) != len(levels)
            or not set(levels).issubset(ACCESS_LEVELS)
        ):
            raise SchemaError("store allowed_access is invalid")
        entries[store_id] = entry
    if not required_store_ids.issubset(entries):
        raise QueryError("unknown_store", "query references an unknown store")

    resolved_roots = {
        store_id: _resolve_store_root(registry_path, entry["root"])
        for store_id, entry in entries.items()
    }

    requested_levels = {"public", "controlled"} if allow_controlled else {"public"}
    stores: dict[str, EvidenceStore] = {}
    policies: dict[str, set[str]] = {}
    for store_id in sorted(required_store_ids):
        entry = entries[store_id]
        effective = set(entry["allowed_access"]) & requested_levels
        if not effective:
            raise QueryError("permission_denied", "store access is not authorized")
        stores[store_id] = EvidenceStore(
            resolved_roots[store_id], read_only=True, meter=meter
        )
        policies[store_id] = effective
    return stores, policies


def _dispatch(service: ChipContextQueryService, request: ParsedQuery) -> dict[str, Any]:
    parameters = request.parameters
    if request.operation == "candidate_status":
        return service.candidate_status(request.scope)
    if request.operation == "candidate_artifacts":
        return service.candidate_artifacts(request.scope, **parameters)
    if request.operation == "failure":
        return service.failure(request.scope, **parameters)
    if request.operation == "compare_metrics":
        return service.compare_metrics(request.scope, **parameters)
    if request.operation == "timing_paths":
        return service.timing_paths(request.scope, **parameters)
    if request.operation == "read_artifact":
        return service.read_artifact(request.scope, **parameters)
    raise SchemaError("query operation is unsupported")


def execute_query(
    registry_path: Path,
    request_path: Path,
    *,
    allow_controlled: bool = False,
) -> QueryExecution:
    started_ns = time.monotonic_ns()
    meter = EvidenceMeter()
    request_value = _read_json_object(request_path, meter, "query request")
    parsed = parse_query_request(request_value)
    registry_value = _read_json_object(registry_path, meter, "store registry")
    stores, policy = load_stores(
        registry_path,
        registry_value,
        required_store_ids=parsed.referenced_store_ids,
        allow_controlled=allow_controlled,
        meter=meter,
    )
    answer = _dispatch(ChipContextQueryService(stores, policy), parsed)
    return QueryExecution(answer=answer, meter=meter, started_ns=started_ns)


def _cost(execution: QueryExecution, wall_time_ns: int) -> dict[str, Any]:
    return {
        "schema_version": QUERY_COST_SCHEMA,
        "wall_time_ns": wall_time_ns,
        **execution.meter.snapshot(),
        "return_bytes": 0,
        "cache_status": "not_configured",
        "physical_io_bytes": None,
        "peak_memory_bytes": None,
    }


def serialize_execution(
    execution: QueryExecution,
    *,
    output_format: str,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
) -> tuple[str, dict[str, Any]]:
    if output_format not in {"json", "markdown"}:
        raise SchemaError("query output format is unsupported")
    if (
        not isinstance(max_output_bytes, int)
        or isinstance(max_output_bytes, bool)
        or not 1 <= max_output_bytes <= MAX_OUTPUT_BYTES
    ):
        raise SchemaError(
            f"max_output_bytes must be between 1 and {MAX_OUTPUT_BYTES}"
        )

    # Include one complete render in the internal query wall time, then freeze
    # dynamic timing before solving the return_bytes fixed point.
    preliminary_cost = _cost(
        execution, max(0, time.monotonic_ns() - execution.started_ns)
    )
    if output_format == "json":
        render_json({
            "schema_version": QUERY_RESPONSE_SCHEMA,
            "answer": execution.answer,
            "cost": preliminary_cost,
        })
    else:
        render_markdown(execution.answer, preliminary_cost)
    wall_time_ns = max(0, time.monotonic_ns() - execution.started_ns)
    cost = _cost(execution, wall_time_ns)
    rendered = ""
    for _ in range(16):
        if output_format == "json":
            rendered = render_json({
                "schema_version": QUERY_RESPONSE_SCHEMA,
                "answer": execution.answer,
                "cost": cost,
            })
        else:
            rendered = render_markdown(execution.answer, cost)
        actual = len(rendered.encode("utf-8"))
        if cost["return_bytes"] == actual:
            break
        cost["return_bytes"] = actual
    else:
        raise RuntimeError("query response byte accounting did not converge")
    if len(rendered.encode("utf-8")) > max_output_bytes:
        raise QueryError("budget_exceeded", "query output exceeds the byte budget")
    return rendered, cost


__all__ = [
    "DEFAULT_OUTPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "ParsedQuery",
    "QUERY_COST_SCHEMA",
    "QUERY_REQUEST_SCHEMA",
    "QUERY_RESPONSE_SCHEMA",
    "QueryExecution",
    "STORE_REGISTRY_SCHEMA",
    "execute_query",
    "load_stores",
    "parse_query_request",
    "serialize_execution",
]
