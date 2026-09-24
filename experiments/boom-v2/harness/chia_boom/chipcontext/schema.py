from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any


MISSING_REASONS = frozenset({
    "not_run",
    "not_collected",
    "parse_failed",
    "not_comparable",
    "not_supported",
    "inconclusive",
})
AVAILABILITY = frozenset({"available", *MISSING_REASONS})
CHECK_OUTCOMES = frozenset({"pass", "fail", "inconclusive"})
WORKING_STATES = frozenset({
    "not_evaluated", "evaluated_current", "evaluated_other_revision"
})
MAPPING_QUALITIES = frozenset({"exact", "derived", "ambiguous", "unmapped"})
ACCESS_LEVELS = frozenset({"public", "controlled"})
KNOWN_UNITS = frozenset({"ns", "count", "cycles", "bytes", "ratio"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SchemaError(ValueError):
    """A record violates the ChipContext v1 evidence contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def require_sha256(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SchemaError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def hashed_record(schema_version: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not schema_version.startswith("chipcontext."):
        raise SchemaError("ChipContext schema_version must use chipcontext.*")
    record = {"schema_version": schema_version, **payload}
    record["content_hash"] = content_hash(record)
    return record


@dataclass(frozen=True)
class CandidateRef:
    experiment_id: str
    candidate_id: str
    source_sha256: str
    contract_sha256: str
    parent_ref: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.candidate_id:
            raise SchemaError("candidate namespace and ID are required")
        require_sha256("source_sha256", self.source_sha256)
        require_sha256("contract_sha256", self.contract_sha256)
        if self.parent_ref is not None:
            if not self.parent_ref.get("candidate_id"):
                raise SchemaError("parent_ref.candidate_id is required")
            require_sha256(
                "parent_ref.source_sha256", self.parent_ref.get("source_sha256", "")
            )

    @property
    def ref_id(self) -> str:
        return content_hash(self.to_dict(include_ref=False))

    def to_dict(self, *, include_ref: bool = True) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        if include_ref:
            value["ref_id"] = self.ref_id
        return value


@dataclass(frozen=True)
class WorkingState:
    working_source_sha256: str
    last_evaluated_ref: str | None
    last_evaluated_source_sha256: str | None
    state: str

    def __post_init__(self) -> None:
        require_sha256("working_source_sha256", self.working_source_sha256)
        if self.last_evaluated_ref is not None:
            require_sha256("last_evaluated_ref", self.last_evaluated_ref)
        if self.last_evaluated_source_sha256 is not None:
            require_sha256(
                "last_evaluated_source_sha256",
                self.last_evaluated_source_sha256,
            )
        if self.state not in WORKING_STATES:
            raise SchemaError(f"unknown working state: {self.state}")
        if self.last_evaluated_ref is None and self.state != "not_evaluated":
            raise SchemaError("a non-evaluated working tree cannot inherit measurements")

    @classmethod
    def derive(
        cls,
        *,
        working_source_sha256: str,
        candidate: CandidateRef | None,
    ) -> "WorkingState":
        if candidate is None:
            return cls(working_source_sha256, None, None, "not_evaluated")
        state = (
            "evaluated_current"
            if working_source_sha256 == candidate.source_sha256
            else "evaluated_other_revision"
        )
        return cls(
            working_source_sha256,
            candidate.ref_id,
            candidate.source_sha256,
            state,
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class ArtifactRef:
    ref_id: str
    sha256: str
    kind: str
    root_id: str
    location: str
    owner_ref: str
    attempt_id: str
    size_bytes: int
    access: str
    media_type: str = "text/plain"

    def __post_init__(self) -> None:
        require_sha256("artifact ref_id", self.ref_id)
        require_sha256("artifact sha256", self.sha256)
        require_sha256("artifact owner_ref", self.owner_ref)
        if not self.kind or not self.root_id or not self.location or not self.attempt_id:
            raise SchemaError("artifact kind, root, location, and attempt are required")
        if self.location.startswith("/") or ".." in self.location.split("/"):
            raise SchemaError("artifact location must be a normalized relative path")
        if self.size_bytes < 0:
            raise SchemaError("artifact size cannot be negative")
        if self.access not in ACCESS_LEVELS:
            raise SchemaError(f"unknown artifact access: {self.access}")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Measurement:
    metric_id: str
    definition_revision: str
    value: int | float | None
    unit: str
    scope: dict[str, Any]
    provenance: str
    source_ref: str
    availability: str
    mapping_quality: str = "exact"

    def __post_init__(self) -> None:
        if not self.metric_id or not self.definition_revision or not self.provenance:
            raise SchemaError(
                "measurement metric, definition, and provenance are required"
            )
        require_sha256("measurement source_ref", self.source_ref)
        if not isinstance(self.scope, dict):
            raise SchemaError("measurement scope must be an object")
        if self.availability not in AVAILABILITY:
            raise SchemaError(f"unknown availability: {self.availability}")
        if self.unit not in KNOWN_UNITS:
            raise SchemaError(f"unknown measurement unit: {self.unit}")
        if self.mapping_quality not in MAPPING_QUALITIES:
            raise SchemaError(f"unknown mapping quality: {self.mapping_quality}")
        if self.availability == "available":
            if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
                raise SchemaError("available measurements require a numeric value")
            if not math.isfinite(float(self.value)):
                raise SchemaError("measurement value must be finite")
        elif self.value is not None:
            raise SchemaError("unavailable measurements must have a null value")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CheckRecord:
    check_id: str
    executed: bool
    outcome: str | None
    scope: dict[str, Any]
    source_ref: str | None

    def __post_init__(self) -> None:
        if not self.check_id or not isinstance(self.scope, dict):
            raise SchemaError("check ID and object scope are required")
        if self.executed:
            if self.outcome not in CHECK_OUTCOMES:
                raise SchemaError("executed checks require a concrete outcome")
            if not self.source_ref:
                raise SchemaError("executed checks require a source reference")
            require_sha256("check source_ref", self.source_ref)
        elif self.outcome is not None:
            raise SchemaError("unexecuted checks cannot report pass or fail")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
