from __future__ import annotations

import json
import os
import tempfile
import time
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


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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

    def __init__(self, root: Path, artifact_roots: dict[str, Path] | None = None):
        self.root = root.resolve()
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
        if not roots_path.is_file():
            raise FileNotFoundError(f"evidence root registry is missing: {roots_path}")
        self.artifact_roots = {
            key: Path(value) for key, value in json.loads(roots_path.read_text()).items()
        }

    def write_record(
        self, kind: str, schema_version: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        record = hashed_record(schema_version, payload)
        path = self.root / "records" / kind / f"{record['content_hash']}.json"
        _publish(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
        return record

    def publish_alias(self, name: str, record: dict[str, Any] | str) -> None:
        if "/" in name or name.startswith("."):
            raise SchemaError("alias must be a top-level filename")
        text = (
            record
            if isinstance(record, str)
            else json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
        _publish(self.root / name, text)

    def append_event(self, event: dict[str, Any]) -> Path:
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
        digest = _sha256_file(resolved)
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
        if not path.is_file():
            raise KeyError(f"unknown artifact reference: {ref_id}")
        payload = json.loads(path.read_text())
        ref = ArtifactRef(**payload)
        unsigned = {key: value for key, value in payload.items() if key != "ref_id"}
        if content_hash(unsigned) != ref.ref_id or ref.ref_id != ref_id:
            raise RuntimeError("artifact reference metadata failed its content hash")
        return ref

    def _artifact_path(self, ref: ArtifactRef, allowed_access: set[str]) -> Path:
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
        if _sha256_file(resolved) != ref.sha256:
            raise RuntimeError("artifact content hash changed after registration")
        return resolved

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
        ref = self.artifact(ref_id)
        access = {"public"} if allowed_access is None else allowed_access
        path = self._artifact_path(ref, access)
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
            with path.open("rb") as handle:
                for number, line in enumerate(handle, 1):
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
                else:
                    byte_offset = path.stat().st_size
            data = b"".join(chunks)
            end_byte = start_byte + len(data)
            if not truncated and end_byte < path.stat().st_size:
                next_cursor = end_byte
        else:
            start_byte = cursor or 0
            if start_byte > path.stat().st_size:
                raise SchemaError("cursor exceeds artifact length")
            with path.open("rb") as handle:
                handle.seek(start_byte)
                data = handle.read(limit_bytes + 1)
            truncated = len(data) > limit_bytes
            data = data[:limit_bytes]
            end_byte = start_byte + len(data)
            next_cursor = end_byte if end_byte < path.stat().st_size else None
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
