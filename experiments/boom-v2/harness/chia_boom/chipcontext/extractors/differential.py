from __future__ import annotations

import json
import re
from typing import Any

from ..schema import SchemaError
from .common import SourceDocument, missing


EXTRACTOR_REVISION = "verilator-differential-v3"
_MISMATCH = re.compile(
    rb"MISMATCH\s+cycle=(?P<cycle>\d+)\s+"
    rb"(?:port|signal)=(?P<signal>\S+)"
    rb"(?:\s+expected=(?P<expected>\S+)\s+actual=(?P<actual>\S+))?",
    re.I,
)
_PASS = re.compile(rb"\bPASS\s+cycles=(\d+)\b", re.I)
_FUNCTIONAL_FAILURE_CLASSES = {
    "functional_mismatch",
    "differential_mismatch",
}


def _load(document: SourceDocument) -> dict[str, Any]:
    try:
        value = json.loads(document.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot parse differential result JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SchemaError("differential result must be a JSON object")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SchemaError(f"{field} must be a non-negative integer")
    return value


def _check(
    check_id: str,
    *,
    executed: bool,
    outcome: str | None,
    source_location: dict[str, Any] | None,
) -> dict[str, Any]:
    if not executed and outcome is not None:
        raise SchemaError(f"unexecuted check {check_id} cannot have an outcome")
    if executed and outcome not in {"pass", "fail", "inconclusive"}:
        raise SchemaError(f"executed check {check_id} needs a valid outcome")
    return {
        "check_id": check_id,
        "executed": executed,
        "outcome": outcome,
        "source_location": source_location,
    }


def _source_refs(
    result: SourceDocument, stdout: SourceDocument | None,
) -> list[str]:
    refs = [result.artifact_ref]
    if stdout is not None:
        refs.append(stdout.artifact_ref)
    return refs


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
    cycles = _nonnegative_integer(value.get("cycles"), "differential cycles")
    seed = _nonnegative_integer(value.get("seed"), "differential seed")
    scenario = value.get("scenario")
    if not isinstance(scenario, str) or not scenario:
        raise SchemaError("differential scenario must be a non-empty string")

    interface_ok = value.get("interface_ok")
    if interface_ok is not None and not isinstance(interface_ok, bool):
        raise SchemaError("differential interface_ok must be boolean when present")
    interface_failure = interface_ok is False
    explicit_failure = value.get("failure_class")
    explicit_functional_failure = explicit_failure in _FUNCTIONAL_FAILURE_CLASSES

    mismatch = _MISMATCH.search(stdout.data) if stdout is not None else None
    pass_line = _PASS.search(stdout.data) if stdout is not None else None
    pass_cycles = int(pass_line.group(1)) if pass_line is not None else None

    returncode_present = "returncode" in value
    phases_present = "directed_phases" in value
    returncode: int | None = None
    phases: list[str] | None = None
    if returncode_present:
        raw_returncode = value["returncode"]
        if type(raw_returncode) is not int:
            raise SchemaError("differential returncode must be an integer")
        returncode = raw_returncode
    elif not interface_failure:
        raise SchemaError("differential returncode must be an integer")
    if phases_present:
        raw_phases = value["directed_phases"]
        if not isinstance(raw_phases, list) or not all(
            isinstance(item, str) for item in raw_phases
        ):
            raise SchemaError("differential directed_phases must be a string list")
        phases = raw_phases
    elif not interface_failure:
        raise SchemaError("differential directed_phases must be a string list")

    run_evidence = []
    if returncode_present:
        run_evidence.append("returncode")
    if phases_present:
        run_evidence.append("directed_phases")
    if mismatch is not None:
        run_evidence.append("stdout_mismatch")
    if pass_line is not None:
        run_evidence.append("stdout_pass")
    if passed:
        run_evidence.append("passed_true")
    if explicit_functional_failure:
        run_evidence.append("explicit_functional_failure")

    execution_state_conflict = interface_failure and bool(run_evidence)
    native_early_exit = interface_failure and not execution_state_conflict
    if execution_state_conflict:
        conflicts.append({
            "field": "differential.execution_state",
            "reason": "conflicting_sources",
            "detail": (
                "interface_ok=false claims pre-simulation exit but run evidence "
                f"is present: {', '.join(run_evidence)}"
            ),
            "source_refs": _source_refs(result, stdout),
        })
    if native_early_exit:
        missing_items.extend([
            missing(
                "differential.return_code",
                "not_run",
                "interface mismatch prevented simulator execution",
            ),
            missing(
                "differential.directed_phases",
                "not_run",
                "interface mismatch prevented directed simulation phases",
            ),
        ])
    else:
        if returncode is None:
            missing_items.append(missing(
                "differential.return_code",
                "not_collected",
                "other evidence implies execution but return code is absent",
            ))
        if phases is None:
            missing_items.append(missing(
                "differential.directed_phases",
                "not_collected",
                "other evidence implies execution but directed phases are absent",
            ))

    if passed and returncode is not None and returncode != 0:
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
            "source_refs": _source_refs(result, stdout),
        })
    if pass_line is not None and not passed:
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": "result says fail while stdout contains a PASS marker",
            "source_refs": _source_refs(result, stdout),
        })
    if explicit_functional_failure and (passed or pass_line is not None):
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": (
                f"functional failure class {explicit_failure!r} conflicts with "
                "pass evidence"
            ),
            "source_refs": _source_refs(result, stdout),
        })
    if pass_cycles is not None and pass_cycles != cycles:
        conflicts.append({
            "field": "differential.completed_cycles",
            "reason": "conflicting_sources",
            "detail": (
                f"configured cycles={cycles} but stdout PASS cycles={pass_cycles}"
            ),
            "source_refs": _source_refs(result, stdout),
        })
    if stdout is not None and passed and pass_line is None:
        conflicts.append({
            "field": "differential.outcome",
            "reason": "conflicting_sources",
            "detail": "result says pass but registered stdout has no PASS marker",
            "source_refs": _source_refs(result, stdout),
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

    grounded_functional_failure = (
        mismatch_fact is not None or explicit_functional_failure
    )
    behavior_executed = not native_early_exit
    if behavior_executed and mismatch_fact is None and not passed:
        missing_items.append(missing(
            "differential.first_mismatch",
            "not_collected",
            "no grounded mismatch marker was found in registered stdout",
        ))

    if conflicts:
        behavior_outcome = "inconclusive" if behavior_executed else None
    elif passed and returncode == 0:
        behavior_outcome = "pass"
    elif grounded_functional_failure:
        behavior_outcome = "fail"
    elif behavior_executed:
        behavior_outcome = "inconclusive"
    else:
        behavior_outcome = None

    interface_outcome: str | None = None
    if interface_ok is not None:
        interface_outcome = (
            "inconclusive"
            if execution_state_conflict
            else ("pass" if interface_ok else "fail")
        )
    interface_check = _check(
        "interface_signature",
        executed=interface_ok is not None,
        outcome=interface_outcome,
        source_location=(
            result.pointer("/interface_ok") if interface_ok is not None else None
        ),
    )
    behavior_check = _check(
        "differential_correctness",
        executed=behavior_executed,
        outcome=behavior_outcome,
        source_location=(result.pointer("/passed") if behavior_executed else None),
    )

    source_locations = {
        "passed": result.pointer("/passed"),
        "seed": result.pointer("/seed"),
        "cycles": result.pointer("/cycles"),
        "scenario": result.pointer("/scenario"),
    }
    if phases is not None:
        source_locations["directed_phases"] = result.pointer("/directed_phases")
    if returncode is not None:
        source_locations["return_code"] = result.pointer("/returncode")
    if interface_ok is not None:
        source_locations["interface_ok"] = result.pointer("/interface_ok")

    if conflicts:
        classification = "conflicting_evidence"
    elif native_early_exit:
        classification = "interface_mismatch"
    elif grounded_functional_failure:
        classification = "functional_mismatch"
    elif behavior_outcome == "pass":
        classification = "passed"
    else:
        classification = "infrastructure_or_unclassified"

    completed_cycles = (
        pass_cycles
        if pass_cycles == cycles and not any(
            item["field"] == "differential.completed_cycles"
            for item in conflicts
        )
        else None
    )
    if completed_cycles is not None and stdout is not None and pass_line is not None:
        source_locations["completed_cycles"] = stdout.location(
            pass_line.start(1), pass_line.end(1)
        )
    if behavior_executed and completed_cycles is None:
        missing_items.append(missing(
            "differential.completed_cycles",
            "not_collected",
            "configured cycle limit is not proof of completed simulation coverage",
        ))

    facts = {
        "check": behavior_check,
        "checks": [interface_check, behavior_check],
        "passed": passed,
        "interface_ok": interface_ok,
        "seed": seed,
        "cycles": cycles,
        "cycles_semantics": "configured_upper_bound",
        "completed_cycles": completed_cycles,
        "scenario": scenario,
        "directed_phases": phases,
        "return_code": returncode,
        "producer_failure_class": explicit_failure,
        "failure_class": classification,
        "first_mismatch": mismatch_fact,
        "source_locations": source_locations,
    }
    simulator_execution = (
        "inconclusive" if execution_state_conflict
        else ("not_run" if native_early_exit else "attempted")
    )
    return {
        "extractor_revision": EXTRACTOR_REVISION,
        "facts": facts,
        "coverage": {
            "result_json": "collected",
            "stdout": "collected" if stdout is not None else "not_collected",
            "simulator_execution": simulator_execution,
            "mismatch_policy": "first_grounded_marker",
            "cycle_count_policy": "stdout_pass_marker_exact_match",
        },
        "missing": missing_items,
        "conflicts": conflicts,
    }
