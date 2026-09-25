from __future__ import annotations

import base64
import dataclasses
import json
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
from .store import DEFAULT_LIMIT_BYTES, MAX_LIMIT_BYTES, EvidenceStore


QUERY_ANSWER_SCHEMA = "chipcontext.query-answer.v1"
QUERY_REVISION = "chipcontext-query-foundation-v2"
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
        conditions: list[dict[str, Any]] | None = None,
        coverage: dict[str, Any] | None = None,
        missing: list[dict[str, Any]] | None = None,
        conflicts: list[dict[str, Any]] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        pagination: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "query": {
                "name": operation,
                "revision": QUERY_REVISION,
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
