#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from chia_boom.chipcontext.query_cli import execute_query, serialize_execution


OPERATIONS = (
    "candidate_status",
    "candidate_artifacts",
    "failure",
    "compare_metrics",
    "timing_paths",
    "read_artifact",
)


def stats(values: list[int]) -> dict[str, int | float]:
    ordered = sorted(values)
    if len(ordered) < 2:
        q1 = q3 = float(ordered[0])
    else:
        q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return {
        "count": len(ordered),
        "min": ordered[0],
        "q1": q1,
        "median": statistics.median(ordered),
        "q3": q3,
        "iqr": q3 - q1,
        "max": ordered[-1],
    }


def benchmark(
    suite: Path,
    *,
    in_process_repeats: int,
    cli_repeats: int,
) -> dict[str, Any]:
    registry = suite / "trusted-stores.json"
    suite_record = json.loads((suite / "SUITE.json").read_text())
    operations: dict[str, Any] = {}
    for operation in OPERATIONS:
        request = suite / "requests" / f"{operation}.json"
        api_wall: list[int] = []
        costs: list[dict[str, Any]] = []
        for _ in range(in_process_repeats):
            started = time.monotonic_ns()
            execution = execute_query(registry, request)
            _, cost = serialize_execution(
                execution, output_format="json", max_output_bytes=64 * 1024
            )
            api_wall.append(time.monotonic_ns() - started)
            costs.append(cost)
        cli_wall: list[int] = []
        for _ in range(cli_repeats):
            started = time.monotonic_ns()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "chia_boom.chipcontext.cli",
                    "query",
                    "--registry",
                    str(registry),
                    "--request",
                    str(request),
                    "--format",
                    "json",
                    "--max-output-bytes",
                    str(64 * 1024),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            cli_wall.append(time.monotonic_ns() - started)
            if completed.returncode:
                raise RuntimeError(
                    f"CLI benchmark failed for {operation}: {completed.stderr.strip()}"
                )
        counter_names = (
            "configuration_read_bytes",
            "store_metadata_read_bytes",
            "record_read_bytes",
            "artifact_metadata_read_bytes",
            "artifact_scan_bytes",
            "hash_bytes",
            "parse_bytes",
            "return_bytes",
        )
        operations[operation] = {
            "in_process_wall_time_ns": stats(api_wall),
            "cli_process_wall_time_ns": stats(cli_wall),
            "response_phase_time_ns": {
                name: stats([
                    int(value["phase_time_ns"][name]) for value in costs
                ])
                for name in sorted(costs[0]["phase_time_ns"])
            },
            "logical_work": {
                name: stats([int(value[name]) for value in costs])
                for name in counter_names
            },
        }
    return {
        "schema_version": "chipcontext.query-benchmark.v1",
        "suite": {
            "large_report_bytes": suite_record["large_report_bytes"],
            "operation_count": len(OPERATIONS),
            "in_process_repeats": in_process_repeats,
            "cli_repeats": cli_repeats,
        },
        "measurement": {
            "clock": "time.monotonic_ns",
            "quartiles": "statistics.quantiles(n=4, method=inclusive)",
            "logical_bytes_are_not_physical_io": True,
            "response_cost_boundary": "through_first_complete_render",
            "phase_times_are_disjoint": True,
            "performance_threshold": None,
        },
        "operations": operations,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--in-process-repeats", type=int, default=10)
    parser.add_argument("--cli-repeats", type=int, default=5)
    args = parser.parse_args()
    if args.in_process_repeats < 1 or args.cli_repeats < 1:
        raise SystemExit("repeat counts must be positive")
    result = benchmark(
        args.suite.resolve(),
        in_process_repeats=args.in_process_repeats,
        cli_repeats=args.cli_repeats,
    )
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "schema_version": result["schema_version"],
        "operation_count": len(result["operations"]),
        "output": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
