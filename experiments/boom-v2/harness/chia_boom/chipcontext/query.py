from __future__ import annotations

import base64
import dataclasses
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .schema import (
    ACCESS_LEVELS,
    ArtifactRef,
    CandidateRef,
    CheckRecord,
    SchemaError,
    WorkingState,
    content_hash,
    hashed_record,
    require_sha256,
)
from .recipes import (
    METRIC_DEFINITIONS,
    STAGE_MAP,
    baseline_measurement,
    compare_metric_sets,
    comparison_conditions,
    measurement_from_value,
    metric_map,
)
from .store import DEFAULT_LIMIT_BYTES, MAX_LIMIT_BYTES, EvidenceStore


QUERY_ANSWER_SCHEMA = "chipcontext.query-answer.v1"
QUERY_REVISION = "chipcontext-query-foundation-v2"
DOMAIN_QUERY_REVISION = "chipcontext-query-domain-v1"
QUERY_CURSOR_SCHEMA = "chipcontext.query-cursor.v1"
ARTIFACT_STAGE_MAP_REVISION = "artifact-stage-map-v1"
STORE_REGISTRY_SCHEMA = "chipcontext.store-registry.v1"
_STORE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class QueryError(RuntimeError):
    """A fail-closed query error with a stable, non-path-bearing category."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EvidenceHandle:
    store_id: str
    snapshot_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.store_id, str) or not _STORE_ID.fullmatch(self.store_id):
            raise SchemaError("store_id must be a normalized logical identifier")
        require_sha256("snapshot_ref", self.snapshot_ref)

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class QueryScope:
    handle: EvidenceHandle
    expected_candidate: CandidateRef | Mapping[str, Any] | None = None
    expected_attempt_id: str | None = None
    working_source_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.handle, EvidenceHandle):
            raise SchemaError("query scope requires an EvidenceHandle")
        if self.expected_attempt_id is not None and not self.expected_attempt_id:
            raise SchemaError("expected_attempt_id cannot be empty")
        if self.working_source_sha256 is not None:
            require_sha256("working_source_sha256", self.working_source_sha256)
        if self.expected_candidate is not None:
            object.__setattr__(
                self, "expected_candidate", _candidate_from_value(self.expected_candidate)
            )

    def normalized_expectations(self) -> dict[str, Any]:
        candidate = (
            _candidate_from_value(self.expected_candidate).to_dict()
            if self.expected_candidate is not None
            else None
        )
        return {
            "candidate_ref": candidate,
            "attempt_id": self.expected_attempt_id,
            "working_source_sha256": self.working_source_sha256,
        }


@dataclass(frozen=True)
class RegisteredStore:
    store_id: str
    store: EvidenceStore
    allowed_access: frozenset[str]


class TrustedStoreRegistry:
    """Trusted in-process registry; query payloads never construct entries."""

    schema_version = STORE_REGISTRY_SCHEMA

    def __init__(
        self,
        stores: Mapping[str, EvidenceStore],
        access_policy: Mapping[str, Iterable[str]],
    ) -> None:
        if not stores:
            raise SchemaError("at least one trusted evidence store is required")
        entries: dict[str, RegisteredStore] = {}
        for store_id, store in stores.items():
            if not _STORE_ID.fullmatch(store_id) or not isinstance(store, EvidenceStore):
                raise SchemaError(
                    "trusted stores must use normalized IDs and EvidenceStore values"
                )
            levels = frozenset(access_policy.get(store_id, ()))
            if not levels or not levels.issubset(ACCESS_LEVELS):
                raise SchemaError("every store requires an explicit valid access policy")
            entries[store_id] = RegisteredStore(store_id, store, levels)
        extras = set(access_policy) - set(stores)
        if extras:
            raise SchemaError("access policy references an unknown store")
        self._entries = entries

    def get(self, store_id: str) -> RegisteredStore | None:
        return self._entries.get(store_id)

    def describe(self) -> dict[str, Any]:
        """Return only logical IDs and permissions; filesystem paths stay private."""
        return hashed_record(self.schema_version, {
            "stores": [
                {
                    "store_id": entry.store_id,
                    "allowed_access": sorted(entry.allowed_access),
                }
                for entry in sorted(self._entries.values(), key=lambda item: item.store_id)
            ],
        })


@dataclass(frozen=True)
class ResolvedEvidence:
    store: EvidenceStore
    allowed_access: frozenset[str]
    scope: QueryScope
    snapshot: dict[str, Any]
    manifest: dict[str, Any]
    candidate: CandidateRef
    artifacts: dict[str, ArtifactRef]
    extractions: dict[str, dict[str, Any]]

    @property
    def attempt_id(self) -> str:
        return self.manifest["attempt_id"]

    @property
    def applicability(self) -> dict[str, Any]:
        selected = self.candidate.source_sha256
        working = self.scope.working_source_sha256
        if working is None:
            status = "selected_evaluation"
        elif working == selected:
            status = "current"
        else:
            status = "historical"
        return {
            "status": status,
            "evaluated_source_sha256": selected,
            "working_source_sha256": working,
        }

    def _artifact_dependencies(self, refs: Iterable[str]) -> set[str]:
        """Expand artifact/extraction sources to their authorized raw bytes."""
        dependencies: set[str] = set()
        for ref_id in set(refs):
            if ref_id in self.artifacts:
                dependencies.add(ref_id)
                continue
            extraction = self.extractions.get(ref_id)
            if extraction is None:
                raise QueryError(
                    "integrity_error", "required evidence source is not in the snapshot"
                )
            for item in extraction["inputs"]:
                dependencies.add(item["artifact_ref"])
        return dependencies

    def authorize_artifacts(self, refs: Iterable[str]) -> None:
        unique = sorted(set(refs))
        for ref_id in unique:
            ref = self.artifacts.get(ref_id)
            if ref is None:
                raise QueryError("integrity_error", "required artifact is not in the manifest")
            if ref.access not in self.allowed_access:
                raise QueryError("permission_denied", "required evidence is not authorized")
        for ref_id in unique:
            try:
                self.store.verified_artifact_bytes(
                    ref_id, allowed_access=set(self.allowed_access)
                )
            except PermissionError as exc:
                raise QueryError(
                    "permission_denied", "required evidence is not authorized"
                ) from exc
            except (FileNotFoundError, KeyError, RuntimeError, SchemaError) as exc:
                raise QueryError(
                    "integrity_error", "required artifact failed integrity validation"
                ) from exc

    def authorize_sources(self, refs: Iterable[str]) -> set[str]:
        dependencies = self._artifact_dependencies(refs)
        self.authorize_artifacts(dependencies)
        return dependencies

    def authorize_envelope(self) -> set[str]:
        """Authorize every dependency required to reveal the query envelope."""
        return self.authorize_sources([
            *self.snapshot.get("source_refs", []),
            *self.snapshot.get("extraction_refs", []),
        ])

    def describe_sources(self, refs: Iterable[str]) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for ref_id in sorted(set(refs)):
            if ref_id in self.artifacts:
                values.append({"type": "artifact", "ref": ref_id})
            elif ref_id in self.extractions:
                values.append({
                    "type": "extraction",
                    "ref": ref_id,
                    "artifact_dependencies": sorted(
                        item["artifact_ref"]
                        for item in self.extractions[ref_id]["inputs"]
                    ),
                })
            else:
                raise QueryError(
                    "integrity_error", "required evidence source is not in the snapshot"
                )
        return values


def _candidate_from_value(value: CandidateRef | Mapping[str, Any]) -> CandidateRef:
    if isinstance(value, CandidateRef):
        return value
    if not isinstance(value, Mapping):
        raise SchemaError("expected_candidate must be a CandidateRef or object")
    try:
        candidate = CandidateRef(
            experiment_id=value["experiment_id"],
            candidate_id=value["candidate_id"],
            source_sha256=value["source_sha256"],
            contract_sha256=value["contract_sha256"],
            parent_ref=value.get("parent_ref"),
        )
    except (KeyError, TypeError) as exc:
        raise SchemaError("candidate reference is incomplete") from exc
    embedded = value.get("ref_id")
    if embedded is not None and embedded != candidate.ref_id:
        raise SchemaError("candidate reference failed its content identity")
    return candidate


def _read_record(
    store: EvidenceStore,
    kind: str,
    ref_id: str,
    *,
    user_selected: bool,
) -> dict[str, Any]:
    try:
        return store.read_record(kind, ref_id)
    except KeyError as exc:
        code = "unknown_reference" if user_selected else "integrity_error"
        raise QueryError(code, f"{kind} record is unavailable") from exc
    except (RuntimeError, SchemaError, json.JSONDecodeError) as exc:
        raise QueryError("integrity_error", f"{kind} record failed integrity validation") from exc


def _artifact_stage(ref: ArtifactRef, manifest: Mapping[str, Any]) -> dict[str, str]:
    kind = ref.kind
    fixed = (
        ("elaboration_", "elaboration"),
        ("differential_", "correctness"),
        ("post_synth_", "post_synth"),
        ("post_route_", "post_route"),
        ("regression_", "regression"),
    )
    if kind == "evaluation_record":
        stage = manifest.get("normalized_stage")
        if stage not in {"elaboration", "correctness", "post_synth", "post_route", "regression"}:
            stage = "unknown"
        return {"stage": stage, "basis": "evaluation_manifest"}
    for prefix, stage in fixed:
        if kind.startswith(prefix):
            return {"stage": stage, "basis": ARTIFACT_STAGE_MAP_REVISION}
    return {"stage": "unknown", "basis": ARTIFACT_STAGE_MAP_REVISION}


def _encode_cursor(binding: str, offset: int) -> str:
    payload = {
        "schema_version": QUERY_CURSOR_SCHEMA,
        "binding": binding,
        "offset": offset,
    }
    envelope = {**payload, "checksum": content_hash(payload)}
    raw = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, binding: str) -> int:
    if not isinstance(cursor, str) or not cursor:
        raise SchemaError("cursor must be a non-empty string")
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(base64.b64decode(cursor + padding, altchars=b"-_", validate=True))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SchemaError("cursor is malformed") from exc
    if not isinstance(value, dict) or value.get("schema_version") != QUERY_CURSOR_SCHEMA:
        raise SchemaError("cursor schema is unsupported")
    unsigned = {key: item for key, item in value.items() if key != "checksum"}
    if value.get("checksum") != content_hash(unsigned):
        raise SchemaError("cursor failed its content identity")
    if value.get("binding") != binding:
        raise SchemaError("cursor does not belong to this query")
    offset = value.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SchemaError("cursor offset is invalid")
    return offset


def _read_utf8_artifact_page(
    resolved: ResolvedEvidence,
    artifact_ref: str,
    *,
    start_line: int | None,
    line_count: int | None,
    cursor: int | None,
    limit_bytes: int,
) -> dict[str, Any]:
    """Read an exact UTF-8 page without replacing or splitting code points."""
    if (
        not isinstance(limit_bytes, int)
        or isinstance(limit_bytes, bool)
        or not 1 <= limit_bytes <= MAX_LIMIT_BYTES
    ):
        raise SchemaError(f"limit_bytes must be between 1 and {MAX_LIMIT_BYTES}")
    if cursor is not None and start_line is not None:
        raise SchemaError("cursor and line selection are mutually exclusive")
    if line_count is not None and start_line is None:
        raise SchemaError("line_count requires start_line")
    if cursor is not None and cursor < 0:
        raise SchemaError("cursor cannot be negative")
    try:
        _, artifact_data = resolved.store.verified_artifact_bytes(
            artifact_ref, allowed_access=set(resolved.allowed_access)
        )
    except PermissionError as exc:
        raise QueryError(
            "permission_denied", "required evidence is not authorized"
        ) from exc
    except (FileNotFoundError, KeyError, RuntimeError, SchemaError) as exc:
        raise QueryError(
            "integrity_error", "artifact failed integrity validation"
        ) from exc
    try:
        artifact_data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise QueryError(
            "invalid_text_encoding", "artifact is not valid UTF-8 text"
        ) from exc

    actual_start_line: int | None = None
    actual_end_line: int | None = None
    if start_line is not None:
        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(line_count, int)
            or isinstance(line_count, bool)
            or start_line < 1
            or line_count < 1
        ):
            raise SchemaError("line queries require positive start_line/line_count")
        lines = artifact_data.splitlines(keepends=True)
        prefix = lines[: start_line - 1]
        selected = lines[start_line - 1 : start_line - 1 + line_count]
        start_byte = sum(len(line) for line in prefix)
        selection_end = start_byte + sum(len(line) for line in selected)
        if selected:
            actual_start_line = start_line
    else:
        start_byte = cursor or 0
        selection_end = len(artifact_data)

    if start_byte > len(artifact_data):
        raise SchemaError("cursor exceeds artifact length")
    if start_byte < len(artifact_data) and artifact_data[start_byte] & 0xC0 == 0x80:
        raise SchemaError("cursor is not on a UTF-8 character boundary")

    end_byte = min(start_byte + limit_bytes, selection_end)
    while (
        end_byte > start_byte
        and end_byte < len(artifact_data)
        and artifact_data[end_byte] & 0xC0 == 0x80
    ):
        end_byte -= 1
    if end_byte == start_byte and start_byte < selection_end:
        raise QueryError(
            "text_page_too_small",
            "limit_bytes cannot contain the next UTF-8 character",
        )

    data = artifact_data[start_byte:end_byte]
    if actual_start_line is not None and data:
        complete_lines = len(data.splitlines(keepends=True))
        if data.endswith((b"\n", b"\r")) or end_byte == selection_end:
            actual_end_line = actual_start_line + complete_lines - 1
        elif complete_lines > 1:
            actual_end_line = actual_start_line + complete_lines - 2
    truncated = end_byte < selection_end
    return {
        "schema_version": "chipcontext.bounded-artifact.v1",
        "content": data.decode("utf-8", errors="strict"),
        "span": {
            "start_byte": start_byte,
            "end_byte": end_byte,
            "start_line": actual_start_line,
            "end_line": actual_end_line,
        },
        "truncated": truncated,
        "next_cursor": end_byte if end_byte < len(artifact_data) else None,
    }


class EvidenceResolver:
    """Resolve one explicit immutable evidence handle without alias inference."""

    def __init__(
        self,
        stores: Mapping[str, EvidenceStore],
        access_policy: Mapping[str, Iterable[str]],
    ) -> None:
        self.registry = TrustedStoreRegistry(stores, access_policy)

    def resolve(self, scope: QueryScope) -> ResolvedEvidence:
        if not isinstance(scope, QueryScope):
            raise SchemaError("query requires a QueryScope")
        registration = self.registry.get(scope.handle.store_id)
        if registration is None:
            raise QueryError("unknown_store", "evidence store is not registered")
        store = registration.store
        snapshot = _read_record(
            store, "snapshots", scope.handle.snapshot_ref, user_selected=True
        )
        if snapshot.get("schema_version") != "chipcontext.evidence-snapshot.v1":
            raise QueryError("integrity_error", "snapshot schema is unsupported")
        manifest_ref = snapshot.get("evaluation_manifest_ref")
        try:
            require_sha256("evaluation_manifest_ref", manifest_ref)
        except SchemaError as exc:
            raise QueryError("integrity_error", "snapshot manifest reference is invalid") from exc
        manifest = _read_record(store, "manifests", manifest_ref, user_selected=False)
        if manifest.get("schema_version") != "chipcontext.evaluation-manifest.v1":
            raise QueryError("integrity_error", "manifest schema is unsupported")

        try:
            snapshot_candidate = _candidate_from_value(snapshot["candidate_ref"])
            manifest_candidate = _candidate_from_value(manifest["candidate_ref"])
        except (KeyError, SchemaError) as exc:
            raise QueryError("integrity_error", "candidate binding is invalid") from exc
        if snapshot_candidate != manifest_candidate:
            raise QueryError("integrity_error", "snapshot and manifest candidates differ")
        if manifest.get("contract_sha256") != manifest_candidate.contract_sha256:
            raise QueryError("integrity_error", "manifest contract binding is invalid")
        attempt_id = manifest.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise QueryError("integrity_error", "manifest attempt is invalid")
        if not isinstance(manifest.get("completion_status"), str):
            raise QueryError("integrity_error", "manifest completion status is invalid")
        if not isinstance(manifest.get("binding_status"), dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in manifest["binding_status"].items()
        ):
            raise QueryError("integrity_error", "manifest binding status is invalid")

        if scope.expected_candidate is not None:
            expected = _candidate_from_value(scope.expected_candidate)
            if expected != manifest_candidate:
                raise QueryError("scope_mismatch", "candidate does not match query scope")
        if (
            scope.expected_attempt_id is not None
            and scope.expected_attempt_id != attempt_id
        ):
            raise QueryError("scope_mismatch", "attempt does not match query scope")

        artifact_rows = manifest.get("artifacts")
        if not isinstance(artifact_rows, list):
            raise QueryError("integrity_error", "manifest artifacts are invalid")
        artifacts: dict[str, ArtifactRef] = {}
        for row in artifact_rows:
            try:
                if not isinstance(row, dict):
                    raise SchemaError("artifact entry is not an object")
                ref = ArtifactRef(**row)
                stored = store.artifact(ref.ref_id)
            except (TypeError, KeyError, RuntimeError, SchemaError) as exc:
                raise QueryError("integrity_error", "artifact reference is invalid") from exc
            if stored != ref:
                raise QueryError("integrity_error", "artifact metadata does not match manifest")
            if ref.owner_ref != manifest_candidate.ref_id or ref.attempt_id != attempt_id:
                raise QueryError("integrity_error", "artifact ownership does not match manifest")
            if ref.ref_id in artifacts:
                raise QueryError("integrity_error", "manifest contains duplicate artifact references")
            artifacts[ref.ref_id] = ref

        source_refs = snapshot.get("source_refs")
        if not isinstance(source_refs, list) or any(
            not isinstance(item, str) for item in source_refs
        ):
            raise QueryError("integrity_error", "snapshot source references are invalid")
        if len(source_refs) != len(set(source_refs)) or not set(source_refs).issubset(artifacts):
            raise QueryError("integrity_error", "snapshot source closure is invalid")
        try:
            working_state = WorkingState(**snapshot["working_state"])
        except (KeyError, TypeError, SchemaError) as exc:
            raise QueryError("integrity_error", "snapshot working state is invalid") from exc
        if (
            working_state.last_evaluated_ref != manifest_candidate.ref_id
            or working_state.last_evaluated_source_sha256
            != manifest_candidate.source_sha256
        ):
            raise QueryError("integrity_error", "snapshot working state is misbound")
        for field in ("missing", "conflicts"):
            if not isinstance(snapshot.get(field), list) or any(
                not isinstance(item, dict) for item in snapshot[field]
            ):
                raise QueryError("integrity_error", f"snapshot {field} is invalid")

        manifest_extractions = manifest.get("extraction_refs", [])
        snapshot_extractions = snapshot.get("extraction_refs", [])
        if (
            not isinstance(manifest_extractions, list)
            or not isinstance(snapshot_extractions, list)
            or manifest_extractions != snapshot_extractions
            or len(manifest_extractions) != len(set(manifest_extractions))
        ):
            raise QueryError("integrity_error", "extraction binding is invalid")
        extractions: dict[str, dict[str, Any]] = {}
        for extraction_ref in manifest_extractions:
            try:
                require_sha256("extraction_ref", extraction_ref)
            except SchemaError as exc:
                raise QueryError("integrity_error", "extraction reference is invalid") from exc
            record = _read_record(
                store, "extractions", extraction_ref, user_selected=False
            )
            if (
                record.get("schema_version") != "chipcontext.extraction.v1"
                or record.get("owner_ref") != manifest_candidate.ref_id
                or record.get("attempt_id") != attempt_id
            ):
                raise QueryError("integrity_error", "extraction ownership is invalid")
            inputs = record.get("inputs")
            if not isinstance(inputs, list) or not inputs:
                raise QueryError("integrity_error", "extraction inputs are invalid")
            for item in inputs:
                if not isinstance(item, dict):
                    raise QueryError("integrity_error", "extraction input is invalid")
                ref = artifacts.get(item.get("artifact_ref"))
                if (
                    ref is None
                    or item.get("artifact_sha256") != ref.sha256
                    or item.get("kind") != ref.kind
                ):
                    raise QueryError("integrity_error", "extraction input binding is invalid")
            extractions[extraction_ref] = record

        # Facts can be grounded directly in a registered artifact or in a
        # versioned extraction.  Extraction records remain safe because their
        # owner/attempt and every raw input were validated above.
        fact_sources = set(source_refs) | set(extractions)
        for collection in (snapshot.get("checks", []), snapshot.get("measurements", [])):
            if not isinstance(collection, list):
                raise QueryError("integrity_error", "snapshot fact collection is invalid")
            for row in collection:
                if not isinstance(row, dict):
                    raise QueryError("integrity_error", "snapshot fact is invalid")
                source_ref = row.get("source_ref")
                if source_ref is not None and source_ref not in fact_sources:
                    raise QueryError(
                        "integrity_error", "snapshot fact source is outside its closure"
                    )

        return ResolvedEvidence(
            store=store,
            allowed_access=registration.allowed_access,
            scope=scope,
            snapshot=snapshot,
            manifest=manifest,
            candidate=manifest_candidate,
            artifacts=artifacts,
            extractions=extractions,
        )


def _measurement_index(
    snapshot: Mapping[str, Any], *, stage: str
) -> tuple[dict[str, Any], set[str]]:
    """Validate and index measurements without resolving duplicate evidence."""
    rows = snapshot.get("measurements")
    if not isinstance(rows, list):
        raise QueryError("integrity_error", "snapshot measurements are invalid")
    indexed: dict[str, Any] = {}
    conflicts = _conflicted_metrics(snapshot)
    for raw in rows:
        try:
            measurement = measurement_from_value(raw)
        except SchemaError as exc:
            raise QueryError("integrity_error", "snapshot measurement is invalid") from exc
        if measurement.scope.get("stage") != stage:
            continue
        if measurement.metric_id in indexed:
            conflicts.add(measurement.metric_id)
            continue
        indexed[measurement.metric_id] = measurement
    return indexed, conflicts


def _conflicted_metrics(snapshot: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    conflicts = snapshot.get("conflicts", [])
    if not isinstance(conflicts, list):
        raise QueryError("integrity_error", "snapshot conflicts are invalid")
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            raise QueryError("integrity_error", "snapshot conflict is invalid")
        affected = conflict.get("affected_metrics", [])
        if isinstance(affected, list):
            values.update(item for item in affected if isinstance(item, str))
        field = conflict.get("field")
        if isinstance(field, str) and field.startswith("measurements."):
            values.add(field.split(".", 1)[1])
    return values


def _validate_metric_ids(metric_ids: Iterable[str]) -> list[str]:
    if isinstance(metric_ids, (str, bytes)):
        raise SchemaError("metric_ids must be a collection of strings")
    try:
        values = sorted(set(metric_ids))
    except TypeError as exc:
        raise SchemaError("metric_ids must be a collection of strings") from exc
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise SchemaError("metric_ids must contain non-empty strings")
    return values


def _nonnegative_int_or_none(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise QueryError("integrity_error", f"{field} is invalid")
    return value


def _validated_timing_paths(
    paths: Any, *, stage: str, allowed_artifact_refs: set[str]
) -> list[dict[str, Any]]:
    if not isinstance(paths, list):
        raise QueryError("integrity_error", "timing path facts are invalid")
    ranks: set[int] = set()
    validated: list[dict[str, Any]] = []
    for value in paths:
        if not isinstance(value, dict):
            raise QueryError("integrity_error", "timing path fact is invalid")
        rank = value.get("rank")
        if type(rank) is not int or rank < 1 or rank in ranks:
            raise QueryError("integrity_error", "timing path rank is invalid")
        ranks.add(rank)
        if value.get("stage") != stage:
            raise QueryError("integrity_error", "timing path stage is invalid")
        if value.get("availability") not in {"available", "partial", "parse_failed"}:
            raise QueryError("integrity_error", "timing path availability is invalid")
        slack = value.get("slack_ns")
        if slack is not None and (
            not isinstance(slack, (int, float))
            or isinstance(slack, bool)
            or not math.isfinite(float(slack))
        ):
            raise QueryError("integrity_error", "timing path slack is invalid")
        for field in ("source", "destination", "path_group", "path_type"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise QueryError("integrity_error", "timing path field is invalid")
        location = value.get("source_location")
        if not isinstance(location, dict) or not isinstance(
            location.get("artifact_ref"), str
        ):
            raise QueryError("integrity_error", "timing path source is invalid")
        if location["artifact_ref"] not in allowed_artifact_refs:
            raise QueryError("integrity_error", "timing path source is outside extraction")
        validated.append(value)
    return validated


class ChipContextQueryService:
    """Deterministic, read-only queries over sealed ChipContext evidence."""

    def __init__(
        self,
        stores: Mapping[str, EvidenceStore],
        access_policy: Mapping[str, Iterable[str]],
    ) -> None:
        self.resolver = EvidenceResolver(stores, access_policy)

    @staticmethod
    def _answer(
        resolved: ResolvedEvidence,
        *,
        operation: str,
        parameters: dict[str, Any],
        result: Any,
        conditions: list[dict[str, Any]] | dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        missing: list[dict[str, Any]] | None = None,
        conflicts: list[dict[str, Any]] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        pagination: dict[str, Any] | None = None,
        revision: str = QUERY_REVISION,
    ) -> dict[str, Any]:
        payload = {
            "query": {
                "name": operation,
                "revision": revision,
                "parameters": parameters,
            },
            "resolved_scope": {
                "store_id": resolved.scope.handle.store_id,
                "snapshot_ref": resolved.snapshot["content_hash"],
                "manifest_ref": resolved.manifest["content_hash"],
                "candidate_ref": resolved.candidate.to_dict(),
                "attempt_id": resolved.attempt_id,
            },
            "applicability": resolved.applicability,
            "result": result,
            "conditions": conditions or [],
            "coverage": coverage or {},
            "missing": missing or [],
            "conflicts": conflicts or [],
            "source_refs": source_refs or [],
            "pagination": pagination or {"truncated": False, "next_cursor": None},
        }
        return hashed_record(QUERY_ANSWER_SCHEMA, payload)

    def candidate_status(self, scope: QueryScope) -> dict[str, Any]:
        resolved = self.resolver.resolve(scope)
        resolved.authorize_envelope()
        checks = resolved.snapshot.get("checks")
        if not isinstance(checks, list):
            raise QueryError("integrity_error", "snapshot checks are invalid")
        check_records: list[dict[str, Any]] = []
        required_refs: set[str] = set()
        for value in checks:
            try:
                check = CheckRecord(**value)
            except (TypeError, SchemaError) as exc:
                raise QueryError("integrity_error", "snapshot check is invalid") from exc
            check_records.append(check.to_dict())
            if check.source_ref:
                required_refs.add(check.source_ref)
        # Status returns snapshot-level missing/conflict information as well as
        # checks.  Conservatively require the complete snapshot source closure
        # so a derived JSON record cannot downgrade a controlled dependency.
        required_refs.update(resolved.snapshot.get("source_refs", []))
        required_refs.update(resolved.snapshot.get("extraction_refs", []))
        resolved.authorize_sources(required_refs)
        sources = resolved.describe_sources(required_refs)
        return self._answer(
            resolved,
            operation="candidate_status",
            parameters={},
            result={
                "completion_status": resolved.manifest.get("completion_status"),
                "binding_status": resolved.manifest.get("binding_status"),
                "raw_stage": resolved.manifest.get("raw_stage"),
                "normalized_stage": resolved.manifest.get("normalized_stage"),
                "checks": check_records,
                "recorded_working_state": resolved.snapshot.get("working_state"),
                "acceptance_scope": "recorded_checks_only",
                "final_chip_acceptance": "not_assessed",
                "acceptance_note": (
                    "Passing recorded checks does not establish final chip acceptance."
                ),
            },
            conditions=[
                {
                    "kind": "evidence_contract",
                    "contract_sha256": resolved.candidate.contract_sha256,
                    "parser_revision": resolved.manifest.get("parser_revision"),
                }
            ],
            coverage={
                "check_count": len(check_records),
                "executed_count": sum(bool(row["executed"]) for row in check_records),
            },
            missing=list(resolved.snapshot.get("missing", [])),
            conflicts=list(resolved.snapshot.get("conflicts", [])),
            source_refs=sources,
        )

    def get_candidate_status(self, scope: QueryScope) -> dict[str, Any]:
        """Issue #11 logical API name; delegates to the fixed operation."""
        return self.candidate_status(scope)

    def candidate_artifacts(
        self,
        scope: QueryScope,
        *,
        stage: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SchemaError("limit must be between 1 and 100")
        if stage is not None and (
            not isinstance(stage, str) or stage not in {
                "elaboration", "correctness", "post_synth", "post_route",
                "regression", "unknown",
            }
        ):
            raise SchemaError("stage filter is invalid")
        if kinds is not None and isinstance(kinds, (str, bytes)):
            raise SchemaError("artifact kinds must be a collection of strings")
        try:
            normalized_kinds = sorted(set(kinds or ()))
        except TypeError as exc:
            raise SchemaError("artifact kinds must be a collection of strings") from exc
        if any(not isinstance(kind, str) or not kind for kind in normalized_kinds):
            raise SchemaError("artifact kinds must be non-empty strings")
        resolved = self.resolver.resolve(scope)
        # The answer envelope itself reveals candidate identity and evaluation
        # metadata, so it is authorized before filters can produce an empty set.
        resolved.authorize_envelope()
        rows = []
        for ref in resolved.artifacts.values():
            stage_info = _artifact_stage(ref, resolved.manifest)
            if stage is not None and stage_info["stage"] != stage:
                continue
            if normalized_kinds and ref.kind not in normalized_kinds:
                continue
            rows.append((stage_info["stage"], ref.kind, ref.ref_id, ref, stage_info))
        rows.sort(key=lambda item: item[:3])
        # Filtering defines this operation's dependency closure.  Authorize the
        # entire matching set before returning counts or the first page.
        resolved.authorize_artifacts(row[3].ref_id for row in rows)
        binding_payload = {
            "operation": "candidate_artifacts",
            "revision": QUERY_REVISION,
            "scope": {
                "handle": scope.handle.to_dict(),
                "expectations": scope.normalized_expectations(),
            },
            "filters": {"stage": stage, "kinds": normalized_kinds},
            "order": ["stage", "kind", "ref"],
            "input_hash": content_hash(
                {
                    "snapshot": resolved.snapshot["content_hash"],
                    "manifest": resolved.manifest["content_hash"],
                }
            ),
        }
        binding = content_hash(binding_payload)
        offset = _decode_cursor(cursor, binding) if cursor is not None else 0
        if offset > len(rows):
            raise SchemaError("cursor exceeds the result set")
        page = rows[offset:offset + limit]
        items = [
            {
                "content_ref": ref.ref_id,
                "kind": ref.kind,
                "stage": stage_info["stage"],
                "stage_basis": stage_info["basis"],
                "size_bytes": ref.size_bytes,
                "media_type": ref.media_type,
            }
            for _, _, _, ref, stage_info in page
        ]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor(binding, next_offset) if next_offset < len(rows) else None
        )
        return self._answer(
            resolved,
            operation="candidate_artifacts",
            parameters={
                "stage": stage,
                "kinds": normalized_kinds,
                "limit": limit,
                "page_offset": offset,
            },
            result={"artifacts": items},
            coverage={
                "matching_count": len(rows),
                "returned_count": len(items),
                "stage_map_revision": ARTIFACT_STAGE_MAP_REVISION,
            },
            source_refs=[
                {"type": "artifact", "ref": item["content_ref"]} for item in items
            ],
            pagination={
                "truncated": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        )

    def list_candidate_artifacts(
        self,
        scope: QueryScope,
        *,
        stage: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Issue #11 logical API name; delegates to the fixed operation."""
        return self.candidate_artifacts(
            scope, stage=stage, kinds=kinds, limit=limit, cursor=cursor
        )

    def failure(
        self,
        scope: QueryScope,
        *,
        check: str,
        extraction_ref: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(check, str) or not check:
            raise SchemaError("failure query requires a check")
        if extraction_ref is not None:
            require_sha256("extraction_ref", extraction_ref)
        resolved = self.resolver.resolve(scope)
        resolved.authorize_envelope()
        checks: dict[str, CheckRecord] = {}
        for raw in resolved.snapshot.get("checks", []):
            try:
                record = CheckRecord(**raw)
            except (TypeError, SchemaError) as exc:
                raise QueryError("integrity_error", "snapshot check is invalid") from exc
            checks[record.check_id] = record
        recorded = checks.get(check)

        differential = {
            ref: value for ref, value in resolved.extractions.items()
            if value.get("extractor", {}).get("name") == "verilator_differential"
        }
        selected_ref: str | None = None
        selected: dict[str, Any] | None = None
        if extraction_ref is not None:
            selected = differential.get(extraction_ref)
            if selected is None:
                raise QueryError(
                    "scope_mismatch",
                    "extraction is not a differential result for this attempt",
                )
            selected_ref = extraction_ref
        elif len(differential) > 1:
            raise QueryError(
                "ambiguous_reference",
                "multiple differential extractions require an explicit reference",
            )
        elif differential:
            selected_ref, selected = next(iter(differential.items()))

        required: set[str] = set()
        if recorded is not None and recorded.source_ref is not None:
            required.add(recorded.source_ref)
        if selected_ref is not None:
            required.add(selected_ref)
        resolved.authorize_sources(required)

        missing = list(resolved.snapshot.get("missing", []))
        conflicts = list(resolved.snapshot.get("conflicts", []))
        observation: dict[str, Any] = {
            "availability": "not_collected",
            "configured_cycles": None,
            "completed_cycles": None,
            "seed": None,
            "scenario": None,
            "directed_phases": None,
            "return_code": None,
            "failure_class": None,
            "first_mismatch": None,
            "extracted_check": None,
        }
        coverage: dict[str, Any] = {
            "selected_extraction_ref": selected_ref,
            "differential_extraction_count": len(differential),
        }
        if selected is not None:
            if selected.get("extractor", {}).get("revision") != "verilator-differential-v3":
                raise QueryError(
                    "unsupported_evidence", "differential extraction revision is unsupported"
                )
            facts = selected.get("facts")
            if not isinstance(facts, dict):
                raise QueryError("integrity_error", "differential facts are invalid")
            extracted_checks = facts.get("checks")
            if not isinstance(extracted_checks, list):
                extracted_checks = [facts.get("check")]
            extracted = next(
                (
                    value for value in extracted_checks
                    if isinstance(value, dict) and value.get("check_id") == check
                ),
                None,
            )
            mismatch = facts.get("first_mismatch")
            if mismatch is not None and not isinstance(mismatch, dict):
                raise QueryError("integrity_error", "mismatch observation is invalid")
            if mismatch is not None:
                location = mismatch.get("source_location")
                input_refs = {
                    value.get("artifact_ref") for value in selected.get("inputs", [])
                    if isinstance(value, dict)
                }
                if (
                    not isinstance(location, dict)
                    or location.get("artifact_ref") not in input_refs
                ):
                    raise QueryError(
                        "integrity_error", "mismatch source is outside extraction"
                    )
            selected_conflicts = selected.get("conflicts", [])
            selected_missing = selected.get("missing", [])
            if not isinstance(selected_conflicts, list) or not isinstance(
                selected_missing, list
            ):
                raise QueryError("integrity_error", "differential status is invalid")
            conflicts.extend(dict(value) for value in selected_conflicts)
            missing.extend(dict(value) for value in selected_missing)
            availability = "available"
            if selected_conflicts:
                availability = "inconclusive"
            elif extracted is None:
                availability = "not_collected"
            observation = {
                "availability": availability,
                "configured_cycles": _nonnegative_int_or_none(
                    facts.get("cycles"), "configured cycles"
                ),
                "completed_cycles": _nonnegative_int_or_none(
                    facts.get("completed_cycles"), "completed cycles"
                ),
                "seed": _nonnegative_int_or_none(facts.get("seed"), "seed"),
                "scenario": facts.get("scenario"),
                "directed_phases": facts.get("directed_phases"),
                "return_code": facts.get("return_code"),
                "failure_class": facts.get("failure_class"),
                "first_mismatch": mismatch,
                "extracted_check": extracted,
            }
            coverage.update(selected.get("coverage", {}))
            if recorded is not None and extracted is not None and (
                recorded.executed != extracted.get("executed")
                or recorded.outcome != extracted.get("outcome")
            ):
                conflict = {
                    "field": f"checks.{check}",
                    "reason": "conflicting_sources",
                    "detail": "snapshot and differential extraction disagree",
                    "source_refs": sorted(filter(None, [recorded.source_ref, selected_ref])),
                }
                conflicts.append(conflict)
                observation["availability"] = "inconclusive"

        if recorded is None:
            missing.append({
                "field": f"checks.{check}",
                "reason": "not_collected",
                "detail": "the selected snapshot has no record for this check",
            })
        source_refs = resolved.describe_sources(required)
        mismatch = observation.get("first_mismatch")
        if isinstance(mismatch, dict) and isinstance(mismatch.get("source_location"), dict):
            source_refs.append({
                "type": "source_location",
                "ref": mismatch["source_location"].get("artifact_ref"),
                "location": mismatch["source_location"],
            })
        return self._answer(
            resolved,
            operation="failure",
            revision=DOMAIN_QUERY_REVISION,
            parameters={"check": check, "extraction_ref": extraction_ref},
            result={
                "recorded_check": recorded.to_dict() if recorded is not None else None,
                "observation": observation,
                "verdict_is_distinct_from_observation": True,
            },
            coverage=coverage,
            missing=missing,
            conflicts=conflicts,
            source_refs=source_refs,
        )

    def get_failure_bundle(
        self,
        scope: QueryScope,
        *,
        check: str,
        extraction_ref: str | None = None,
    ) -> dict[str, Any]:
        return self.failure(scope, check=check, extraction_ref=extraction_ref)

    def compare_metrics(
        self,
        scope: QueryScope,
        *,
        reference: str | QueryScope,
        stage: str,
        metric_ids: Iterable[str],
    ) -> dict[str, Any]:
        if stage not in {"post_synth", "post_route"}:
            raise SchemaError("metric comparison stage must be post_synth or post_route")
        requested = _validate_metric_ids(metric_ids)
        current = self.resolver.resolve(scope)
        current.authorize_envelope()
        current_metrics, current_conflicts = _measurement_index(
            current.snapshot, stage=stage
        )
        current_sources = {
            value.source_ref for value in current_metrics.values()
            if value.metric_id in requested
        }
        current.authorize_sources(current_sources)
        current_conditions = comparison_conditions(current_metrics.values())

        reference_metrics: dict[str, Any]
        reference_conflicts: set[str]
        reference_conditions: dict[str, Any]
        reference_sources: set[str]
        reference_scope: dict[str, Any]
        reference_ineligibility_reason: str | None = None
        source_refs = [
            {"store_id": scope.handle.store_id, **value}
            for value in current.describe_sources(current_sources)
        ]
        if reference == "bound_baseline":
            baseline_refs = [
                value for value in current.artifacts.values()
                if value.kind == "baseline_measurement"
            ]
            if len(baseline_refs) != 1:
                raise QueryError(
                    "ambiguous_reference" if baseline_refs else "unknown_reference",
                    "bound baseline must resolve to exactly one registered artifact",
                )
            baseline_ref = baseline_refs[0]
            current.authorize_artifacts([baseline_ref.ref_id])
            try:
                _, data = current.store.verified_artifact_bytes(
                    baseline_ref.ref_id,
                    allowed_access=set(current.allowed_access),
                )
                baseline = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise QueryError("integrity_error", "baseline record is invalid") from exc
            if not isinstance(baseline, dict):
                raise QueryError("integrity_error", "baseline record is invalid")
            raw_metrics = metric_map(baseline)
            raw_stage = baseline.get("stage")
            normalized_stage = STAGE_MAP.get(raw_stage) if isinstance(raw_stage, str) else None
            bound = current.manifest.get("binding_status", {}).get("baseline") == "verified"
            qualified = (
                current.manifest.get("binding_status", {}).get("qualification")
                == "verified"
            )
            if not bound:
                reference_ineligibility_reason = "baseline_binding_not_verified"
            elif not qualified:
                reference_ineligibility_reason = "qualification_binding_not_verified"
            reference_conditions = {
                "stage": normalized_stage,
                "part": baseline.get("part", current_conditions.get("part"))
                if bound else baseline.get("part"),
                "clock_period_ns": baseline.get(
                    "clock_period_ns",
                    current_conditions.get("clock_period_ns") if bound else None,
                ),
                "tool_fingerprint": baseline.get(
                    "tool_fingerprint",
                    current_conditions.get("tool_fingerprint")
                    if bound and qualified else None,
                ),
                "reference_fingerprint": baseline.get(
                    "reference_fingerprint",
                    current_conditions.get("reference_fingerprint") if bound else None,
                ),
            }
            reference_metrics = {
                metric_id: baseline_measurement(
                    baseline, metric_id, baseline_ref.ref_id, reference_conditions
                )
                for metric_id in raw_metrics
            }
            reference_conflicts = set()
            reference_sources = {baseline_ref.ref_id}
            reference_scope = {
                "kind": "bound_baseline",
                "content_ref": baseline_ref.ref_id,
                "binding_status": current.manifest.get("binding_status", {}).get(
                    "baseline"
                ),
            }
            source_refs.append({
                "store_id": scope.handle.store_id,
                "type": "artifact",
                "ref": baseline_ref.ref_id,
            })
        elif isinstance(reference, QueryScope):
            reference_resolved = self.resolver.resolve(reference)
            reference_resolved.authorize_envelope()
            reference_metrics, reference_conflicts = _measurement_index(
                reference_resolved.snapshot, stage=stage
            )
            reference_sources = {
                value.source_ref for value in reference_metrics.values()
                if value.metric_id in requested
            }
            reference_resolved.authorize_sources(reference_sources)
            reference_conditions = comparison_conditions(reference_metrics.values())
            reference_scope = {
                "kind": "snapshot",
                "store_id": reference.handle.store_id,
                "snapshot_ref": reference_resolved.snapshot["content_hash"],
                "candidate_ref": reference_resolved.candidate.to_dict(),
                "attempt_id": reference_resolved.attempt_id,
                "applicability": reference_resolved.applicability,
            }
            if (
                reference_resolved.candidate.contract_sha256
                != current.candidate.contract_sha256
            ):
                reference_ineligibility_reason = "contract_differs"
            source_refs.extend(
                {"store_id": reference.handle.store_id, **value}
                for value in reference_resolved.describe_sources(reference_sources)
            )
        else:
            raise SchemaError(
                "reference must be 'bound_baseline' or an explicit QueryScope"
            )

        conflicted = current_conflicts | reference_conflicts
        comparisons = compare_metric_sets(
            reference_metrics,
            current_metrics,
            metric_ids=requested,
            reference_conditions=reference_conditions,
            current_conditions=current_conditions,
            conflicted_metric_ids=conflicted,
            ineligibility_reason=reference_ineligibility_reason,
        )
        statuses = [value["status"] for value in comparisons]
        missing: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for value in comparisons:
            if value["status"] == "conflict":
                conflicts.append({
                    "field": f"measurements.{value['metric_id']}",
                    "reason": "conflicting_sources",
                    "detail": "at least one selected snapshot marks this metric conflicted",
                })
            elif value["status"] != "comparable":
                missing.append({
                    "field": f"measurements.{value['metric_id']}",
                    "reason": (
                        "not_collected" if value["status"] == "missing"
                        else "not_comparable"
                    ),
                    "detail": value.get("reason"),
                })
        return self._answer(
            current,
            operation="compare_metrics",
            revision=DOMAIN_QUERY_REVISION,
            parameters={
                "reference": reference_scope,
                "stage": stage,
                "metric_ids": requested,
                "delta_direction": "current_minus_reference",
            },
            result={
                "comparisons": comparisons,
                "all_comparable": bool(comparisons)
                and all(status == "comparable" for status in statuses),
                "any_comparable": any(status == "comparable" for status in statuses),
            },
            conditions={
                "reference": reference_conditions,
                "current": current_conditions,
            },
            coverage={
                "requested_metric_ids": requested,
                "known_metric_ids": sorted(METRIC_DEFINITIONS),
            },
            missing=missing,
            conflicts=conflicts,
            source_refs=source_refs,
        )

    def timing_paths(
        self,
        scope: QueryScope,
        *,
        extraction_ref: str,
        stage: str,
        path_group: str | None = None,
        source: str | None = None,
        destination: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        require_sha256("extraction_ref", extraction_ref)
        if stage not in {"post_synth", "post_route"}:
            raise SchemaError("timing path stage must be post_synth or post_route")
        for name, value in (
            ("path_group", path_group), ("source", source),
            ("destination", destination),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise SchemaError(f"{name} must be a non-empty exact string")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SchemaError("limit must be between 1 and 100")
        resolved = self.resolver.resolve(scope)
        resolved.authorize_envelope()
        extraction = resolved.extractions.get(extraction_ref)
        if extraction is None or extraction.get("extractor", {}).get("name") != "vivado":
            raise QueryError(
                "scope_mismatch", "extraction is not a Vivado result for this attempt"
            )
        if extraction.get("extractor", {}).get("revision") != "vivado-2024.1-v3":
            raise QueryError(
                "unsupported_evidence", "Vivado extraction revision is unsupported"
            )
        config_stage = extraction.get("config", {}).get("stage")
        if config_stage != stage:
            raise QueryError("scope_mismatch", "timing path stage does not match extraction")
        resolved.authorize_sources([extraction_ref])
        facts = extraction.get("facts", {})
        input_refs = {
            value.get("artifact_ref") for value in extraction.get("inputs", [])
            if isinstance(value, dict)
        }
        paths = _validated_timing_paths(
            facts.get("timing_paths"),
            stage=stage,
            allowed_artifact_refs=input_refs,
        )
        coverage = extraction.get("coverage", {}).get("timing_paths")
        if not (isinstance(coverage, dict) or coverage == "not_collected"):
            raise QueryError("integrity_error", "timing path coverage is invalid")
        if isinstance(coverage, dict):
            if coverage.get("stage") != stage or coverage.get("parse_status") not in {
                "complete", "partial", "parse_failed"
            }:
                raise QueryError("integrity_error", "timing path coverage is invalid")
            for field in (
                "requested_max_paths", "returned_path_count",
                "reported_block_count", "parsed_slack_count",
            ):
                _nonnegative_int_or_none(coverage.get(field), f"coverage {field}")
            if (
                coverage.get("returned_path_count") != len(paths)
                or coverage.get("reported_block_count") != len(paths)
                or coverage.get("parsed_slack_count")
                != sum(value.get("slack_ns") is not None for value in paths)
            ):
                raise QueryError(
                    "integrity_error", "timing path coverage counts are inconsistent"
                )
            maximum = coverage.get("requested_max_paths")
            if maximum is not None and maximum < len(paths):
                raise QueryError(
                    "integrity_error", "timing path top-k coverage is inconsistent"
                )
            command_location = coverage.get("command_location")
            if command_location is not None and (
                not isinstance(command_location, dict)
                or command_location.get("artifact_ref") not in input_refs
            ):
                raise QueryError(
                    "integrity_error", "timing path command source is invalid"
                )
        filters = {
            "path_group": path_group,
            "source": source,
            "destination": destination,
        }
        matched = [
            value for value in paths
            if all(expected is None or value.get(key) == expected for key, expected in filters.items())
        ]
        report_first = next((value for value in paths if value.get("rank") == 1), None)
        if report_first is None:
            report_first_result = {
                "availability": (
                    "not_collected" if coverage == "not_collected"
                    else coverage.get("parse_status", "parse_failed")
                ),
                "fact": None,
            }
        elif report_first.get("slack_ns") is None:
            report_first_result = {"availability": "parse_failed", "fact": None}
        else:
            report_first_result = {
                "availability": report_first.get("availability", "available"),
                "fact": report_first,
            }
        numeric = [
            value for value in matched
            if isinstance(value.get("slack_ns"), (int, float))
            and not isinstance(value.get("slack_ns"), bool)
        ]
        if numeric:
            filtered_minimum = {
                "availability": "available",
                "fact": min(numeric, key=lambda value: (value["slack_ns"], value["rank"])),
            }
        elif matched:
            filtered_minimum = {"availability": "parse_failed", "fact": None}
        else:
            filtered_minimum = {"availability": "no_match", "fact": None}
        binding = content_hash({
            "operation": "timing_paths",
            "revision": DOMAIN_QUERY_REVISION,
            "scope": {
                "handle": scope.handle.to_dict(),
                "expectations": scope.normalized_expectations(),
            },
            "extraction_ref": extraction_ref,
            "stage": stage,
            "filters": filters,
            "order": "producer_report_rank",
            "input_hash": extraction["content_hash"],
        })
        offset = _decode_cursor(cursor, binding) if cursor is not None else 0
        if offset > len(matched):
            raise SchemaError("cursor exceeds the result set")
        page = matched[offset:offset + limit]
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor(binding, next_offset)
            if next_offset < len(matched) else None
        )
        extraction_conflicts = extraction.get("conflicts", [])
        extraction_missing = extraction.get("missing", [])
        if not isinstance(extraction_conflicts, list) or not isinstance(
            extraction_missing, list
        ):
            raise QueryError("integrity_error", "timing path status is invalid")
        source_refs = resolved.describe_sources([extraction_ref])
        source_refs.extend({
            "type": "source_location",
            "ref": value.get("source_location", {}).get("artifact_ref"),
            "location": value.get("source_location"),
        } for value in page if isinstance(value.get("source_location"), dict))
        return self._answer(
            resolved,
            operation="timing_paths",
            revision=DOMAIN_QUERY_REVISION,
            parameters={
                "extraction_ref": extraction_ref,
                "stage": stage,
                **filters,
                "limit": limit,
                "page_offset": offset,
            },
            result={
                "paths": page,
                "report_first": report_first_result,
                "filtered_minimum": filtered_minimum,
                "global_worst": {
                    "availability": "not_supported",
                    "fact": None,
                    "reason": "a collected top-k report cannot prove global coverage",
                },
            },
            coverage={
                "matching_count": len(matched),
                "returned_count": len(page),
                "filters_are_exact": True,
                "report": coverage,
            },
            missing=list(extraction_missing),
            conflicts=list(extraction_conflicts),
            source_refs=source_refs,
            pagination={
                "truncated": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        )

    def query_timing_paths(self, scope: QueryScope, **kwargs: Any) -> dict[str, Any]:
        return self.timing_paths(scope, **kwargs)

    def read_artifact(
        self,
        scope: QueryScope,
        artifact_ref: str,
        *,
        start_line: int | None = None,
        line_count: int | None = None,
        cursor: str | None = None,
        limit_bytes: int = DEFAULT_LIMIT_BYTES,
    ) -> dict[str, Any]:
        require_sha256("artifact_ref", artifact_ref)
        resolved = self.resolver.resolve(scope)
        # Fail closed on the common metadata envelope before revealing whether
        # the requested reference is part of the selected attempt.
        resolved.authorize_envelope()
        ref = resolved.artifacts.get(artifact_ref)
        if ref is None:
            raise QueryError("scope_mismatch", "artifact is outside the selected attempt")
        binding_payload = {
            "operation": "read_artifact",
            "revision": QUERY_REVISION,
            "scope": {
                "handle": scope.handle.to_dict(),
                "expectations": scope.normalized_expectations(),
            },
            "artifact_ref": artifact_ref,
            "start_line": start_line,
            "line_count": line_count,
            "limit_bytes": limit_bytes,
            "input_hash": ref.sha256,
        }
        binding = content_hash(binding_payload)
        raw_cursor = _decode_cursor(cursor, binding) if cursor is not None else None
        resolved.authorize_artifacts([artifact_ref])
        page = _read_utf8_artifact_page(
            resolved,
            artifact_ref,
            start_line=start_line,
            line_count=line_count,
            cursor=raw_cursor,
            limit_bytes=limit_bytes,
        )
        next_cursor = (
            _encode_cursor(binding, page["next_cursor"])
            if start_line is None and page.get("next_cursor") is not None
            else None
        )
        stage_info = _artifact_stage(ref, resolved.manifest)
        result = {
            "artifact": {
                "content_ref": ref.ref_id,
                "kind": ref.kind,
                "stage": stage_info["stage"],
                "stage_basis": stage_info["basis"],
                "size_bytes": ref.size_bytes,
                "media_type": ref.media_type,
            },
            "content": page["content"],
            "span": page["span"],
        }
        return self._answer(
            resolved,
            operation="read_artifact",
            parameters={
                "artifact_ref": artifact_ref,
                "start_line": start_line,
                "line_count": line_count,
                "limit_bytes": limit_bytes,
                "page_offset": raw_cursor,
            },
            result=result,
            coverage={"returned_bytes": page["span"]["end_byte"] - page["span"]["start_byte"]},
            source_refs=[
                {
                    "type": "artifact",
                    "ref": ref.ref_id,
                    "span": page["span"],
                }
            ],
            pagination={
                "truncated": bool(page["truncated"]),
                "next_cursor": next_cursor,
            },
        )


__all__ = [
    "ARTIFACT_STAGE_MAP_REVISION",
    "ChipContextQueryService",
    "EvidenceHandle",
    "EvidenceResolver",
    "QueryError",
    "QueryScope",
    "RegisteredStore",
    "TrustedStoreRegistry",
]
