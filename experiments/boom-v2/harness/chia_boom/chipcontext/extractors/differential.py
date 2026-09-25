from __future__ import annotations

import json
import re
from typing import Any

from ..schema import SchemaError
from .common import SourceDocument, finite_number, missing


EXTRACTOR_REVISION = "verilator-differential-v1"
_MISMATCH = re.compile(
    rb"MISMATCH\s+cycle=(?P<cycle>\d+)\s+"
    rb"(?:port|signal)=(?P<signal>\S+)"
    rb"(?:\s+expected=(?P<expected>\S+)\s+actual=(?P<actual>\S+))?",
    re.I,
)
_PASS = re.compile(rb"\bPASS\s+cycles=(\d+)\b", re.I)


def _load(document: SourceDocument) -> dict[str, Any]:
    try:
        value = json.loads(document.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot parse differential result JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SchemaError("differential result must be a JSON object")
    return value


def extract_differential(
    *,
    result: SourceDocument,
    stdout: SourceDocument | None,
) -> dict[str, Any]:
    value = _load(result)
    missing_items: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    passed = value.get("passed")
    if not isinstance(passed, bool):
        raise SchemaError("differential passed must be boolean")
    returncode = value.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise SchemaError("differential returncode must be an integer")
    cycles = int(finite_number(value.get("cycles"), "differential cycles"))
    seed = int(finite_number(value.get("seed"), "differential seed"))
    scenario = value.get("scenario")
    if not isinstance(scenario, str) or not scenario:
        raise SchemaError("differential scenario must be a non-empty string")
    phases = value.get("directed_phases")
    if not isinstance(phases, list) or not all(isinstance(item, str) for item in phases):
        raise SchemaError("differential directed_phases must be a string list")
    mismatch = _MISMATCH.search(stdout.data) if stdout is not None else None
    pass_line = _PASS.search(stdout.data) if stdout is not None else None
    if passed and returncode != 0:
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": f"passed=true but returncode={returncode}",
            "source_refs": [result.artifact_ref],
        })
    if mismatch is not None and passed:
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": "result says pass while stdout contains a mismatch",
            "source_refs": [result.artifact_ref, stdout.artifact_ref],
        })
    if pass_line is not None and not passed:
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": "result says fail while stdout contains a PASS marker",
            "source_refs": [result.artifact_ref, stdout.artifact_ref],
        })
    mismatch_fact: dict[str, Any] | None = None
    if mismatch is not None and stdout is not None:
        mismatch_fact = {
            "cycle": int(mismatch.group("cycle")),
            "signal": mismatch.group("signal").decode("utf-8", errors="replace"),
            "expected": (
                mismatch.group("expected").decode("utf-8", errors="replace")
                if mismatch.group("expected") is not None else None
            ),
            "actual": (
                mismatch.group("actual").decode("utf-8", errors="replace")
                if mismatch.group("actual") is not None else None
            ),
            "source_location": stdout.location(mismatch.start(), mismatch.end()),
        }
        for field in ("expected", "actual"):
            if mismatch_fact[field] is None:
                missing_items.append(missing(
                    f"differential.first_mismatch.{field}",
                    "not_collected",
                    f"stdout mismatch marker has no {field} value",
                ))
    elif not passed:
        missing_items.append(missing(
            "differential.first_mismatch",
            "not_collected" if stdout is not None else "not_run",
            "no grounded mismatch marker was found in registered stdout",
        ))
    classification = (
        "functional_mismatch" if mismatch_fact is not None
        else ("passed" if passed and returncode == 0 else "infrastructure_or_unclassified")
    )
    facts = {
        "check": {
            "check_id": "differential_correctness",
            "executed": True,
            "outcome": (
                "inconclusive" if conflicts else ("pass" if passed and returncode == 0 else "fail")
            ),
            "source_location": result.pointer("/passed"),
        },
        "passed": passed,
        "seed": seed,
        "cycles": cycles,
        "scenario": scenario,
        "directed_phases": phases,
        "return_code": returncode,
        "failure_class": classification,
        "first_mismatch": mismatch_fact,
        "source_locations": {
            "passed": result.pointer("/passed"),
            "seed": result.pointer("/seed"),
            "cycles": result.pointer("/cycles"),
            "scenario": result.pointer("/scenario"),
            "directed_phases": result.pointer("/directed_phases"),
            "return_code": result.pointer("/returncode"),
        },
    }
    return {
        "extractor_revision": EXTRACTOR_REVISION,
        "facts": facts,
        "coverage": {
            "result_json": "collected",
            "stdout": "collected" if stdout is not None else "not_collected",
            "mismatch_policy": "first_grounded_marker",
        },
        "missing": missing_items,
        "conflicts": conflicts,
    }
