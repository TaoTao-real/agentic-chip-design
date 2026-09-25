from __future__ import annotations

import hashlib
import math
import re
import shlex
from typing import Any

from ..schema import SchemaError
from .common import SourceDocument, missing


EXTRACTOR_REVISION = "vivado-2024.1-v3"

_SUMMARY = re.compile(
    rb"WNS\(ns\).*?\n\s*-+.*?\n\s*"
    rb"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(\d+)\s+(\d+)",
    re.S,
)
_LUTS = re.compile(rb"\|\s*Slice LUTs\*?\s*\|\s*([\d,]+)")
_REGS = re.compile(rb"\|\s*Slice Registers\s*\|\s*([\d,]+)")
_PATH_BOUNDARY = re.compile(
    rb"^Slack\s+\((?P<status>[^)]+)\)\s*:\s*"
    rb"(?P<raw_slack>[^\r\n]*)",
    re.M,
)
_SLACK_VALUE = re.compile(rb"^\s*(-?\d+(?:\.\d+)?)ns\b")
_COMMAND = re.compile(rb"^\| Command\s*:\s*(.*?)\s*$", re.M)


def _design_summary_matches(data: bytes) -> list[re.Match[bytes]]:
    start = data.find(b"| Design Timing Summary")
    if start < 0:
        start = 0
    end = data.find(b"| Clock Summary", start + 1)
    if end < 0:
        end = len(data)
    values: list[re.Match[bytes]] = []
    cursor = start
    while cursor < end:
        match = _SUMMARY.search(data, cursor, end)
        if match is None:
            break
        values.append(match)
        cursor = match.end()
    return values


def _different(matches: list[re.Match[bytes]], groups: tuple[int, ...]) -> bool:
    return len({tuple(match.group(group) for group in groups) for match in matches}) > 1


def _float(raw: bytes, field: str) -> float:
    value = float(raw.decode("ascii"))
    if not math.isfinite(value):
        raise SchemaError(f"{field} must be finite")
    return value


def _metric(
    metric_id: str,
    revision: str,
    value: int | float,
    unit: str,
    stage: str,
    location: dict[str, Any],
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "definition_revision": revision,
        "value": value,
        "unit": unit,
        "stage": stage,
        "availability": "available",
        "source_location": location,
    }


def parse_ppa_bytes(
    timing_data: bytes,
    utilization_data: bytes,
    period_ns: float,
) -> dict[str, Any]:
    """Compatibility parser used by both BOOM scoring and ChipContext."""

    if not math.isfinite(period_ns) or period_ns <= 0:
        raise SchemaError("clock period must be a finite positive number")
    summaries = _design_summary_matches(timing_data)
    lut_rows = list(_LUTS.finditer(utilization_data))
    reg_rows = list(_REGS.finditer(utilization_data))
    if (
        not summaries or not lut_rows or not reg_rows
        or _different(summaries, (1, 2, 3, 4))
        or _different(lut_rows, (1,))
        or _different(reg_rows, (1,))
    ):
        raise ValueError("cannot parse Vivado timing/utilization reports")
    summary, luts, regs = summaries[0], lut_rows[0], reg_rows[0]
    wns = _float(summary.group(1), "wns_ns")
    return {
        "clock_period_ns": period_ns,
        "wns_ns": wns,
        "critical_delay_ns": period_ns - wns,
        "tns_ns": _float(summary.group(2), "tns_ns"),
        "failing_endpoints": int(summary.group(3)),
        "total_endpoints": int(summary.group(4)),
        "slice_luts": int(luts.group(1).replace(b",", b"")),
        "slice_registers": int(regs.group(1).replace(b",", b"")),
        "timing_report_sha256": hashlib.sha256(timing_data).hexdigest(),
        "utilization_report_sha256": hashlib.sha256(utilization_data).hexdigest(),
    }


def _field(block: bytes, label: bytes) -> re.Match[bytes] | None:
    return re.search(
        rb"^\s*" + re.escape(label) + rb":\s*(.*?)\s*$", block, re.M
    )


def _number_field(block: bytes, label: bytes) -> re.Match[bytes] | None:
    return re.search(
        rb"^\s*" + re.escape(label) + rb":\s*(-?\d+(?:\.\d+)?)ns\b",
        block,
        re.M,
    )


def _command_coverage(document: SourceDocument) -> dict[str, Any]:
    match = _COMMAND.search(document.data)
    if not match:
        return {
            "producer_command": None,
            "requested_max_paths": None,
            "delay_type": None,
            "path_type": None,
            "filters": {},
            "command_location": None,
        }
    command = match.group(1).decode("utf-8", errors="replace")
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    def option(name: str) -> str | None:
        switch = f"-{name}"
        try:
            index = tokens.index(switch)
        except ValueError:
            return None
        return tokens[index + 1] if index + 1 < len(tokens) else None
    maximum = option("max_paths")
    return {
        "producer_command": "report_timing" if "report_timing" in command else None,
        "requested_max_paths": int(maximum) if maximum and maximum.isdigit() else None,
        "delay_type": option("delay_type"),
        "path_type": option("path_type"),
        "filters": {
            key: value for key in ("from", "to", "through", "group", "filter")
            if (value := option(key)) is not None
        },
        "command_location": document.location(match.start(), match.end()),
    }


def parse_timing_paths(
    document: SourceDocument,
    *,
    stage: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    starts = list(_PATH_BOUNDARY.finditer(document.data))
    paths: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []
    invalid_slack_count = 0
    optional = (
        b"Source", b"Destination", b"Path Group", b"Path Type",
        b"Requirement", b"Data Path Delay", b"Logic Levels",
    )
    for index, header in enumerate(starts):
        block_start = header.start()
        block_end = starts[index + 1].start() if index + 1 < len(starts) else len(document.data)
        block = document.data[block_start:block_end]
        slack_match = _SLACK_VALUE.match(header.group("raw_slack"))
        fields = {label: _field(block, label) for label in optional[:4]}
        requirement = _number_field(block, b"Requirement")
        delay = _number_field(block, b"Data Path Delay")
        levels = re.search(rb"^\s*Logic Levels:\s*(\d+)\b", block, re.M)
        absent = [
            label.decode("ascii")
            for label, match in (
                *fields.items(),
                (b"Requirement", requirement),
                (b"Data Path Delay", delay),
                (b"Logic Levels", levels),
            )
            if match is None
        ]
        if absent:
            omitted.append(missing(
                f"timing_paths[{index + 1}]",
                "not_collected",
                "path block lacks optional field(s): " + ", ".join(absent),
            ))
        if slack_match is None:
            invalid_slack_count += 1
            omitted.append(missing(
                f"timing_paths[{index + 1}].slack_ns",
                "parse_failed",
                "path block has an unparseable Slack value",
            ))
        def text(label: bytes) -> str | None:
            match = fields[label]
            return (
                match.group(1).decode("utf-8", errors="replace").strip()
                if match is not None else None
            )
        paths.append({
            "rank": index + 1,
            "stage": stage,
            "availability": (
                "parse_failed" if slack_match is None
                else ("partial" if absent else "available")
            ),
            "missing_fields": (["Slack"] if slack_match is None else []) + absent,
            "status": header.group("status").decode("ascii", errors="replace").lower(),
            "source": text(b"Source"),
            "destination": text(b"Destination"),
            "slack_ns": (
                _float(slack_match.group(1), "timing_path.slack_ns")
                if slack_match is not None else None
            ),
            "raw_slack": header.group("raw_slack").decode(
                "utf-8", errors="replace"
            ).strip(),
            "requirement_ns": (
                _float(requirement.group(1), "timing_path.requirement_ns")
                if requirement is not None else None
            ),
            "data_path_delay_ns": (
                _float(delay.group(1), "timing_path.data_path_delay_ns")
                if delay is not None else None
            ),
            "logic_levels": int(levels.group(1)) if levels is not None else None,
            "path_group": text(b"Path Group"),
            "path_type": text(b"Path Type"),
            "source_location": document.location(block_start, block_end),
        })
    if not starts:
        omitted.append(missing(
            "timing_paths",
            "parse_failed",
            "registered timing-path report has no recognizable Slack path block",
        ))
    coverage = _command_coverage(document)
    coverage.update({
        "stage": stage,
        "returned_path_count": len(paths),
        "reported_block_count": len(starts),
        "parsed_slack_count": len(starts) - invalid_slack_count,
        "parse_status": (
            "parse_failed" if not starts or invalid_slack_count == len(starts)
            else ("partial" if omitted else "complete")
        ),
        "ordering": "producer_report_order",
        "absence_semantics": "not_in_collected_top_k",
    })
    return paths, coverage, omitted


def extract_vivado(
    *,
    timing_summary: SourceDocument,
    utilization: SourceDocument,
    timing_paths: SourceDocument | None,
    period_ns: float,
    stage: str,
) -> dict[str, Any]:
    summaries = _design_summary_matches(timing_summary.data)
    lut_rows = list(_LUTS.finditer(utilization.data))
    reg_rows = list(_REGS.finditer(utilization.data))
    conflicts: list[dict[str, Any]] = []
    if _different(summaries, (1, 2, 3, 4)):
        conflicts.append({
            "field": "vivado.timing_summary",
            "affected_metrics": [
                "wns_ns", "tns_ns", "failing_endpoints",
                "total_endpoints", "critical_delay_ns",
            ],
            "scope": {"stage": stage},
            "reason": "conflicting_sources",
            "detail": "multiple Design Timing Summary rows disagree",
            "source_refs": [timing_summary.artifact_ref],
        })
        summaries = []
    if _different(lut_rows, (1,)):
        conflicts.append({
            "field": "vivado.slice_luts",
            "affected_metrics": ["slice_luts"],
            "scope": {"stage": stage},
            "reason": "conflicting_sources",
            "detail": "multiple Slice LUT rows disagree",
            "source_refs": [utilization.artifact_ref],
        })
        lut_rows = []
    if _different(reg_rows, (1,)):
        conflicts.append({
            "field": "vivado.slice_registers",
            "affected_metrics": ["slice_registers"],
            "scope": {"stage": stage},
            "reason": "conflicting_sources",
            "detail": "multiple Slice Register rows disagree",
            "source_refs": [utilization.artifact_ref],
        })
        reg_rows = []
    summary = summaries[0] if summaries else None
    luts = lut_rows[0] if lut_rows else None
    regs = reg_rows[0] if reg_rows else None
    missing_items: list[dict[str, str]] = []
    if summary is None:
        missing_items.append(missing(
            "vivado.timing_summary", "parse_failed",
            "Design Timing Summary row was not found",
        ))
    if luts is None:
        missing_items.append(missing(
            "vivado.slice_luts", "parse_failed", "Slice LUTs row was not found"
        ))
    if regs is None:
        missing_items.append(missing(
            "vivado.slice_registers", "parse_failed",
            "Slice Registers row was not found",
        ))
    if summary and luts and regs:
        # Keep the fact extractor on the exact scoring parser and fail on any
        # ambiguity that the evaluator would reject.
        parse_ppa_bytes(timing_summary.data, utilization.data, period_ns)
    timing_metrics = (
        ("wns_ns", "timing-wns-v1", "ns", summary, 1),
        ("tns_ns", "timing-tns-v1", "ns", summary, 2),
        ("failing_endpoints", "timing-failing-endpoints-v1", "count", summary, 3),
        ("total_endpoints", "timing-total-endpoints-v1", "count", summary, 4),
    )
    metrics = []
    if summary is not None:
        for metric_id, revision, unit, match, group in timing_metrics:
            value: int | float = (
                int(match.group(group)) if unit == "count"
                else _float(match.group(group), metric_id)
            )
            metrics.append(_metric(
                metric_id, revision, value, unit, stage,
                timing_summary.location(match.start(group), match.end(group)),
            ))
        wns = _float(summary.group(1), "wns_ns")
        metrics.append(_metric(
            "clock_period_ns", "clock-constraint-v1", period_ns, "ns", stage,
            timing_summary.location(summary.start(), summary.end()),
        ))
        metrics.append(_metric(
            "critical_delay_ns", "timing-critical-delay-v1",
            period_ns - wns, "ns", stage,
            timing_summary.location(summary.start(), summary.end()),
        ))
    if luts is not None:
        metrics.append(_metric(
            "slice_luts", "vivado-slice-luts-v1",
            int(luts.group(1).replace(b",", b"")), "count", stage,
            utilization.location(luts.start(1), luts.end(1)),
        ))
    if regs is not None:
        metrics.append(_metric(
            "slice_registers", "vivado-slice-registers-v1",
            int(regs.group(1).replace(b",", b"")), "count", stage,
            utilization.location(regs.start(1), regs.end(1)),
        ))
    paths: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {
        "stage": stage,
        "timing_summary": "collected",
        "utilization": "collected",
        "timing_paths": "not_collected",
    }
    if timing_paths is not None:
        paths, path_coverage, path_missing = parse_timing_paths(
            timing_paths, stage=stage
        )
        coverage["timing_paths"] = path_coverage
        missing_items.extend(path_missing)
    else:
        missing_items.append(missing(
            "timing_paths", "not_collected",
            "no registered timing-path report was supplied",
        ))
    return {
        "extractor_revision": EXTRACTOR_REVISION,
        "facts": {"metrics": metrics, "timing_paths": paths},
        "coverage": coverage,
        "missing": missing_items,
        "conflicts": conflicts,
    }
