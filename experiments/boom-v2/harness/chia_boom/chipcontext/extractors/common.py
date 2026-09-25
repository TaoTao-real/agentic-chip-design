from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

from ..schema import SchemaError


@dataclass(frozen=True)
class SourceDocument:
    """Immutable bytes plus helpers for provenance spans.

    Extractors receive bytes that have already been verified by EvidenceStore.
    Locations therefore point at the exact artifact revision that was parsed.
    """

    artifact_ref: str
    artifact_sha256: str
    data: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", self.data.decode("utf-8", errors="replace"))
        starts = [0]
        for index, value in enumerate(self.data):
            if value == 0x0A:
                starts.append(index + 1)
        object.__setattr__(self, "_line_starts", tuple(starts))

    def location(
        self,
        start_byte: int,
        end_byte: int,
        *,
        json_pointer: str | None = None,
    ) -> dict[str, Any]:
        if start_byte < 0 or end_byte < start_byte or end_byte > len(self.data):
            raise SchemaError("source location is outside the verified artifact")
        starts = self._line_starts
        start_line = bisect.bisect_right(starts, start_byte)
        end_anchor = max(start_byte, end_byte - 1)
        end_line = bisect.bisect_right(starts, end_anchor)
        value: dict[str, Any] = {
            "artifact_ref": self.artifact_ref,
            "artifact_sha256": self.artifact_sha256,
            "byte_span": {"start": start_byte, "end": end_byte},
            "line_span": {"start": start_line, "end": end_line},
        }
        if json_pointer is not None:
            value["json_pointer"] = json_pointer
        return value

    def pointer(self, json_pointer: str) -> dict[str, Any]:
        return {
            "artifact_ref": self.artifact_ref,
            "artifact_sha256": self.artifact_sha256,
            "json_pointer": json_pointer,
        }


def finite_number(value: Any, field: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise SchemaError(f"{field} must be numeric")
    if not math.isfinite(float(value)):
        raise SchemaError(f"{field} must be finite")
    return value


def missing(field: str, reason: str, detail: str) -> dict[str, str]:
    return {"field": field, "reason": reason, "detail": detail}
