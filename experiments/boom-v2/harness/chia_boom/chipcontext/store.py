from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema import (
    ArtifactRef,
    SchemaError,
    canonical_json,
    content_hash,
    hashed_record,
)


DEFAULT_LIMIT_BYTES = 8 * 1024
MAX_LIMIT_BYTES = 64 * 1024


@dataclass
class EvidenceMeter:
    """Logical read and verification work performed by one query."""

    configuration_read_count: int = 0
    configuration_read_bytes: int = 0
    store_metadata_read_count: int = 0
    store_metadata_read_bytes: int = 0
    record_read_count: int = 0
    record_read_bytes: int = 0
    artifact_metadata_read_count: int = 0
    artifact_metadata_read_bytes: int = 0
    artifact_read_count: int = 0
    artifact_scan_bytes: int = 0
    hash_bytes: int = 0
    parse_bytes: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "configuration_read_count": self.configuration_read_count,
            "configuration_read_bytes": self.configuration_read_bytes,
            "store_metadata_read_count": self.store_metadata_read_count,
            "store_metadata_read_bytes": self.store_metadata_read_bytes,
            "record_read_count": self.record_read_count,
            "record_read_bytes": self.record_read_bytes,
            "artifact_metadata_read_count": self.artifact_metadata_read_count,
            "artifact_metadata_read_bytes": self.artifact_metadata_read_bytes,
            "artifact_read_count": self.artifact_read_count,
            "artifact_scan_bytes": self.artifact_scan_bytes,
            "hash_bytes": self.hash_bytes,
            "parse_bytes": self.parse_bytes,
        }


class EvidenceIntegrityError(RuntimeError):
    """Sealed store bytes or metadata violate their integrity contract."""


def _decode_json_object(data: bytes, *, name: str) -> dict[str, Any]:
    """Decode sealed JSON without leaking decoder or filesystem details."""
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceIntegrityError(f"{name} must be a JSON object")
    return value


def _sha256_file(path: Path, meter: EvidenceMeter | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if meter is not None:
                meter.hash_bytes += len(chunk)
    return digest.hexdigest()


def _publish(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() != text:
            raise RuntimeError(f"immutable evidence collision: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text() != text:
                raise RuntimeError(f"immutable evidence collision: {path}")
    finally:
        temporary.unlink(missing_ok=True)


class EvidenceStore:
    """File-backed immutable sidecars over controlled external artifacts."""

    def __init__(
        self,
        root: Path,
        artifact_roots: dict[str, Path] | None = None,
        *,
        read_only: bool = False,
        meter: EvidenceMeter | None = None,
    ):
        if read_only and root.is_symlink():
            raise SchemaError("read-only evidence store may not be a symlink")
        self.root = root.resolve()
        self.read_only = read_only
        self.meter = meter
        if read_only:
            if artifact_roots is not None:
                raise SchemaError("read-only stores cannot publish artifact roots")
            if not self.root.is_dir() or self.root.is_symlink():
                raise FileNotFoundError("evidence store is unavailable")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        roots_path = self.root / "ROOTS.json"
        if artifact_roots is not None:
            normalized = {}
            for name, path in artifact_roots.items():
                if path.is_symlink():
                    raise SchemaError(f"artifact root may not be a symlink: {path}")
                resolved = path.resolve(strict=True)
                if not resolved.is_dir():
                    raise SchemaError(f"artifact root is not a directory: {path}")
                normalized[name] = str(resolved)
            text = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
            _publish(roots_path, text)
        if not roots_path.is_file() or roots_path.is_symlink():
            raise FileNotFoundError("evidence root registry is missing")
        roots_data = roots_path.read_bytes()
        if self.meter is not None:
            self.meter.store_metadata_read_count += 1
            self.meter.store_metadata_read_bytes += len(roots_data)
            self.meter.parse_bytes += len(roots_data)
        roots = _decode_json_object(roots_data, name="evidence root registry")
        artifact_roots_value: dict[str, Path] = {}
        for key, value in roots.items():
            if not isinstance(key, str) or not key or "/" in key or key.startswith("."):
                raise EvidenceIntegrityError(
                    "evidence root registry contains an invalid root ID"
                )
            if not isinstance(value, str) or not value:
                raise EvidenceIntegrityError(
                    "evidence root registry contains an invalid root location"
                )
            artifact_roots_value[key] = Path(value)
        self.artifact_roots = artifact_roots_value

    def _require_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("evidence store is read-only")

    def write_record(
        self, kind: str, schema_version: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_writable()
        record = hashed_record(schema_version, payload)
        path = self.root / "records" / kind / f"{record['content_hash']}.json"
        _publish(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
        return record

    def read_record(self, kind: str, record_hash: str) -> dict[str, Any]:
        """Read a content-addressed ChipContext record and verify its hash."""
        if "/" in kind or kind.startswith("."):
            raise SchemaError("record kind must be a single normalized component")
        path = self.root / "records" / kind / f"{record_hash}.json"
        if not path.is_file() or path.is_symlink():
            raise KeyError(f"unknown {kind} record: {record_hash}")
        data = path.read_bytes()
        if self.meter is not None:
            self.meter.record_read_count += 1
            self.meter.record_read_bytes += len(data)
            self.meter.parse_bytes += len(data)
        record = _decode_json_object(data, name="record")
        embedded = record.get("content_hash")
        unsigned = {key: value for key, value in record.items() if key != "content_hash"}
        try:
            canonical = canonical_json(unsigned).encode("utf-8")
            actual = hashlib.sha256(canonical).hexdigest()
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("record is not canonical JSON") from exc
        if self.meter is not None:
            self.meter.hash_bytes += len(canonical)
        if embedded != record_hash or actual != record_hash:
            raise EvidenceIntegrityError("record failed its content hash")
        return record

    def publish_alias(self, name: str, record: dict[str, Any] | str) -> None:
        self._require_writable()
        if "/" in name or name.startswith("."):
            raise SchemaError("alias must be a top-level filename")
        text = (
            record
            if isinstance(record, str)
            else json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        _publish(self.root / name, text)

    def append_event(self, event: dict[str, Any]) -> Path:
        self._require_writable()
        payload = {
            "schema_version": "chipcontext.event.v1",
            "recorded_unix_ns": time.time_ns(),
            **event,
        }
        digest = content_hash(payload)
        path = self.root / "events" / f"{payload['recorded_unix_ns']}-{digest}.json"
        _publish(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return path

    def register_artifact(
        self,
        path: Path,
        *,
        root_id: str,
        kind: str,
        owner_ref: str,
        attempt_id: str,
        access: str = "public",
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        self._require_writable()
        if root_id not in self.artifact_roots:
            raise SchemaError(f"unknown artifact root: {root_id}")
        root = self.artifact_roots[root_id]
        candidate = path if path.is_absolute() else root / path
        if candidate.is_symlink():
            raise SchemaError("artifact symlinks are not allowed")
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise SchemaError("artifact escapes its controlled root") from exc
        cursor = root
        for component in Path(relative).parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise SchemaError("artifact path traverses a symlink")
        if not resolved.is_file():
            raise SchemaError("artifact is not a regular file")
        digest = _sha256_file(resolved, self.meter)
        payload = {
            "sha256": digest,
            "kind": kind,
            "root_id": root_id,
            "location": relative,
            "owner_ref": owner_ref,
            "attempt_id": attempt_id,
            "size_bytes": resolved.stat().st_size,
            "access": access,
            "media_type": media_type,
        }
        ref = ArtifactRef(ref_id=content_hash(payload), **payload)
        _publish(
            self.root / "artifacts" / f"{ref.ref_id}.json",
            json.dumps(ref.to_dict(), indent=2, sort_keys=True) + "\n",
        )
        return ref

    def artifact(self, ref_id: str) -> ArtifactRef:
        path = self.root / "artifacts" / f"{ref_id}.json"
        if not path.is_file() or path.is_symlink():
            raise KeyError(f"unknown artifact reference: {ref_id}")
        data = path.read_bytes()
        if self.meter is not None:
            self.meter.artifact_metadata_read_count += 1
            self.meter.artifact_metadata_read_bytes += len(data)
            self.meter.parse_bytes += len(data)
        payload = _decode_json_object(data, name="artifact metadata")
        try:
            ref = ArtifactRef(**payload)
        except (TypeError, SchemaError) as exc:
            raise EvidenceIntegrityError("artifact metadata is invalid") from exc
        unsigned = {key: value for key, value in payload.items() if key != "ref_id"}
        try:
            canonical = canonical_json(unsigned).encode("utf-8")
            actual = content_hash(unsigned)
        except (TypeError, ValueError) as exc:
            raise EvidenceIntegrityError("artifact metadata is not canonical JSON") from exc
        if self.meter is not None:
            self.meter.hash_bytes += len(canonical)
        if actual != ref.ref_id or ref.ref_id != ref_id:
            raise EvidenceIntegrityError(
                "artifact reference metadata failed its content hash"
            )
        return ref

    def _artifact_path(
        self,
        ref: ArtifactRef,
        allowed_access: set[str],
        *,
        verify_hash: bool = True,
    ) -> Path:
        if ref.access not in allowed_access:
            raise PermissionError(f"artifact access is not authorized: {ref.access}")
        root = self.artifact_roots.get(ref.root_id)
        if root is None:
            raise SchemaError(f"artifact root is unavailable: {ref.root_id}")
        candidate = root / ref.location
        if candidate.is_symlink():
            raise SchemaError("artifact became a symlink")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise SchemaError("artifact escaped its registered root") from exc
        if verify_hash and _sha256_file(resolved, self.meter) != ref.sha256:
            raise RuntimeError("artifact content hash changed after registration")
        return resolved

    def verified_artifact_bytes(
        self,
        ref_id: str,
        *,
        allowed_access: set[str] | None = None,
    ) -> tuple[ArtifactRef, bytes]:
        """Return the exact bytes whose digest is checked against ArtifactRef.

        The file descriptor is opened without following a final symlink and its
        metadata is checked before and after the read. This makes mutation during
        extraction a hard failure instead of publishing facts about mixed bytes.
        """
        ref = self.artifact(ref_id)
        access = {"public"} if allowed_access is None else allowed_access
        path = self._artifact_path(ref, access, verify_hash=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("artifact changed while it was being read")
        data = b"".join(chunks)
        if self.meter is not None:
            self.meter.artifact_read_count += 1
            self.meter.artifact_scan_bytes += len(data)
            self.meter.hash_bytes += len(data)
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise RuntimeError("artifact content hash changed after registration")
        return ref, data

    def read_artifact(
        self,
        ref_id: str,
        *,
        start_line: int | None = None,
        line_count: int | None = None,
        cursor: int | None = None,
        limit_bytes: int = DEFAULT_LIMIT_BYTES,
        allowed_access: set[str] | None = None,
    ) -> dict[str, Any]:
        if limit_bytes < 1 or limit_bytes > MAX_LIMIT_BYTES:
            raise SchemaError(
                f"limit_bytes must be between 1 and {MAX_LIMIT_BYTES}"
            )
        if cursor is not None and start_line is not None:
            raise SchemaError("cursor and line selection are mutually exclusive")
        if line_count is not None and start_line is None:
            raise SchemaError("line_count requires start_line")
        if cursor is not None and cursor < 0:
            raise SchemaError("cursor cannot be negative")
        access = {"public"} if allowed_access is None else allowed_access
        ref, artifact_data = self.verified_artifact_bytes(
            ref_id, allowed_access=access
        )
        start_byte = 0
        end_byte = 0
        actual_start_line: int | None = None
        actual_end_line: int | None = None
        truncated = False
        next_cursor: int | None = None
        if start_line is not None:
            if start_line < 1 or not line_count or line_count < 1:
                raise SchemaError("line queries require positive start_line/line_count")
            chunks: list[bytes] = []
            selected_bytes = 0
            byte_offset = 0
            for number, line in enumerate(artifact_data.splitlines(keepends=True), 1):
                line_start = byte_offset
                byte_offset += len(line)
                if number < start_line:
                    continue
                if actual_start_line is None:
                    actual_start_line = number
                    start_byte = line_start
                if number >= start_line + line_count:
                    break
                if selected_bytes + len(line) > limit_bytes:
                    remaining = limit_bytes - selected_bytes
                    if remaining:
                        chunks.append(line[:remaining])
                        selected_bytes += remaining
                    truncated = True
                    next_cursor = line_start + max(0, remaining)
                    break
                chunks.append(line)
                selected_bytes += len(line)
                actual_end_line = number
            data = b"".join(chunks)
            end_byte = start_byte + len(data)
            if not truncated and end_byte < len(artifact_data):
                next_cursor = end_byte
        else:
            start_byte = cursor or 0
            if start_byte > len(artifact_data):
                raise SchemaError("cursor exceeds artifact length")
            data = artifact_data[start_byte:start_byte + limit_bytes + 1]
            truncated = len(data) > limit_bytes
            data = data[:limit_bytes]
            end_byte = start_byte + len(data)
            next_cursor = end_byte if end_byte < len(artifact_data) else None
        return {
            "schema_version": "chipcontext.bounded-artifact.v1",
            "artifact_ref": ref.to_dict(),
            "content": data.decode("utf-8", errors="replace"),
            "span": {
                "start_byte": start_byte,
                "end_byte": end_byte,
                "start_line": actual_start_line,
                "end_line": actual_end_line,
            },
            "truncated": truncated,
            "next_cursor": next_cursor,
        }
