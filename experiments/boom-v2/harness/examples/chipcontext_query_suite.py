#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from chia_boom.chipcontext.query import EvidenceHandle, QueryScope
from chia_boom.chipcontext.schema import CandidateRef
from chia_boom.chipcontext.service import ChipContextService
from chia_boom.chipcontext.store import EvidenceStore


FIXTURES = Path(__file__).parents[1] / "chia_boom" / "chipcontext" / "fixtures"


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def scope_value(scope: QueryScope) -> dict[str, Any]:
    candidate = scope.expected_candidate
    return {
        "handle": scope.handle.to_dict(),
        "expected_candidate": (
            candidate.to_dict() if isinstance(candidate, CandidateRef) else candidate
        ),
        "expected_attempt_id": scope.expected_attempt_id,
        "working_source_sha256": scope.working_source_sha256,
    }


def prepare_store(
    output: Path,
    name: str,
    fixture_name: str,
    *,
    domain: bool = False,
    failure_raw: bool = False,
    large_bytes: int = 0,
    access: str = "public",
) -> tuple[EvidenceStore, Any, QueryScope]:
    inputs = output / "inputs" / name
    shutil.copytree(FIXTURES / fixture_name, inputs)
    request_path = inputs / "request.json"
    request = json.loads(request_path.read_text())
    request["access"] = access
    if domain:
        shutil.copy(
            FIXTURES / "extraction" / "post_synth_timing_summary.rpt",
            inputs / "raw-summary.rpt",
        )
        shutil.copy(
            FIXTURES / "extraction" / "post_synth_utilization.rpt",
            inputs / "raw-utilization.rpt",
        )
        shutil.copy(
            FIXTURES / "extraction" / "post_synth_timing_paths.rpt",
            inputs / "raw-paths.rpt",
        )
        request["artifacts"].extend([
            {
                "name": "post_synth_timing_summary",
                "kind": "post_synth_timing_summary",
                "path": "raw-summary.rpt",
            },
            {
                "name": "post_synth_utilization",
                "kind": "post_synth_utilization",
                "path": "raw-utilization.rpt",
            },
            {
                "name": "post_synth_timing_paths",
                "kind": "post_synth_timing_paths",
                "path": "raw-paths.rpt",
            },
        ])
    if failure_raw:
        shutil.copy(
            FIXTURES / "extraction" / "differential-result.json",
            inputs / "raw-differential.json",
        )
        shutil.copy(
            FIXTURES / "extraction" / "differential.stdout",
            inputs / "raw-differential.stdout",
        )
        request["artifacts"].extend([
            {
                "name": "differential_result",
                "kind": "differential_result",
                "path": "raw-differential.json",
            },
            {
                "name": "differential_stdout",
                "kind": "differential_stdout",
                "path": "raw-differential.stdout",
            },
        ])
    if large_bytes:
        line = "0123456789abcdef" * 4 + "\n"
        repeats = (large_bytes + len(line.encode()) - 1) // len(line.encode())
        data = (line * repeats).encode()[:large_bytes]
        (inputs / "large-report.txt").write_bytes(data)
        request["artifacts"].append({
            "name": "large_report",
            "kind": "diagnostic_log",
            "path": "large-report.txt",
        })
    dump(request_path, request)
    store = EvidenceStore(output / "stores" / name, {"input": inputs})
    prepared = ChipContextService(store).prepare(request_path)
    candidate = CandidateRef(**{
        key: value for key, value in prepared.snapshot["candidate_ref"].items()
        if key != "ref_id"
    })
    scope = QueryScope(
        EvidenceHandle(name, prepared.snapshot["content_hash"]),
        expected_candidate=candidate,
        expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        working_source_sha256=candidate.source_sha256,
    )
    return store, prepared, scope


def build_suite(output: Path, large_bytes: int) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    success_store, success, success_scope = prepare_store(
        output, "success", "success", large_bytes=large_bytes
    )
    failure_store, failure, failure_scope = prepare_store(
        output, "failure", "failure", failure_raw=True
    )
    domain_store, domain, domain_scope = prepare_store(
        output, "domain", "success", domain=True
    )
    stores = {
        "success": success_store,
        "failure": failure_store,
        "domain": domain_store,
    }
    dump(output / "trusted-stores.json", {
        "schema_version": "chipcontext.store-registry.v1",
        "stores": [
            {
                "store_id": store_id,
                "root": os.path.relpath(store.root, output.resolve()),
                "allowed_access": ["public"],
            }
            for store_id, store in stores.items()
        ],
    })

    differential_ref = next(
        ref for ref in failure.snapshot["extraction_refs"]
        if failure_store.read_record("extractions", ref)["extractor"]["name"]
        == "verilator_differential"
    )
    vivado_ref = next(
        ref for ref in domain.snapshot["extraction_refs"]
        if domain_store.read_record("extractions", ref)["extractor"]["name"]
        == "vivado"
    )
    if large_bytes:
        read_ref = next(
            row["ref_id"] for row in success.evaluation_manifest["artifacts"]
            if row["kind"] == "diagnostic_log"
        )
    else:
        read_ref = next(
            row["ref_id"] for row in success.evaluation_manifest["artifacts"]
            if row["kind"] == "evaluation_record"
        )
    requests = {
        "candidate_status": (success_scope, {}),
        "candidate_artifacts": (success_scope, {"limit": 20}),
        "failure": (
            failure_scope,
            {
                "check": "differential_correctness",
                "extraction_ref": differential_ref,
            },
        ),
        "compare_metrics": (
            domain_scope,
            {
                "reference": "bound_baseline",
                "stage": "post_synth",
                "metric_ids": ["critical_delay_ns", "slice_luts"],
            },
        ),
        "timing_paths": (
            domain_scope,
            {"extraction_ref": vivado_ref, "stage": "post_synth", "limit": 20},
        ),
        "read_artifact": (
            success_scope,
            {"artifact_ref": read_ref, "limit_bytes": 8192},
        ),
    }
    for operation, (scope, parameters) in requests.items():
        dump(output / "requests" / f"{operation}.json", {
            "schema_version": "chipcontext.query-request.v1",
            "operation": operation,
            "scope": scope_value(scope),
            "parameters": parameters,
        })
    summary = {
        "schema_version": "chipcontext.public-query-suite.v1",
        "large_report_bytes": large_bytes,
        "stores": {
            "success": success.snapshot["content_hash"],
            "failure": failure.snapshot["content_hash"],
            "domain": domain.snapshot["content_hash"],
        },
        "operations": sorted(requests),
    }
    dump(output / "SUITE.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--large-bytes", type=int, default=0)
    args = parser.parse_args()
    if args.large_bytes < 0:
        raise SystemExit("--large-bytes cannot be negative")
    print(json.dumps(build_suite(args.output.resolve(), args.large_bytes), indent=2))


if __name__ == "__main__":
    main()
