#!/usr/bin/env python3
"""Build the public-safe CC-02c C1 acceptance inputs.

The builder creates evidence stores and query requests only.  Expected answers
remain in the separately reviewed corpus fixture and are never derived from a
production query response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from chipcontext_query_suite import build_suite, dump, prepare_store, scope_value


FIXTURE_ROOT = (
    Path(__file__).parents[1]
    / "chia_boom"
    / "chipcontext"
    / "fixtures"
    / "query-acceptance"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_request(
    path: Path, operation: str, scope: Any, parameters: dict[str, Any]
) -> None:
    dump(path, {
        "schema_version": "chipcontext.query-request.v1",
        "operation": operation,
        "scope": scope_value(scope),
        "parameters": parameters,
    })


def build_acceptance_fixture(output: Path) -> dict[str, Any]:
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("output directory must be empty")
    build_suite(output, large_bytes=0)

    _, controlled, controlled_scope = prepare_store(
        output,
        "controlled",
        "success",
        access="controlled",
    )

    # The corrupt store is an intentionally damaged copy of a valid sealed
    # store.  It remains isolated from the valid store so cases cannot affect
    # one another.
    success_root = output / "stores" / "success"
    corrupt_root = output / "stores" / "corrupt"
    shutil.copytree(success_root, corrupt_root)
    (corrupt_root / "ROOTS.json").write_bytes(b"[]")

    registry_path = output / "trusted-stores.json"
    registry = json.loads(registry_path.read_text())
    registry["stores"].extend([
        {
            "store_id": "controlled",
            "root": os.path.relpath(output / "stores" / "controlled", output),
            "allowed_access": ["public", "controlled"],
        },
        {
            "store_id": "corrupt",
            "root": os.path.relpath(corrupt_root, output),
            "allowed_access": ["public"],
        },
    ])
    registry["stores"] = sorted(registry["stores"], key=lambda row: row["store_id"])
    dump(registry_path, registry)

    status_request = json.loads((output / "requests" / "candidate_status.json").read_text())
    success_scope = status_request["scope"]
    request_root = output / "requests" / "acceptance"
    request_root.mkdir(parents=True, exist_ok=True)

    dump(request_root / "normal-comparison.json", {
        "schema_version": "chipcontext.query-request.v1",
        "operation": "compare_metrics",
        "scope": success_scope,
        "parameters": {
            "reference": "bound_baseline",
            "stage": "post_synth",
            "metric_ids": ["critical_delay_ns", "slice_luts"],
        },
    })
    dump(request_root / "missing-stage.json", {
        "schema_version": "chipcontext.query-request.v1",
        "operation": "compare_metrics",
        "scope": success_scope,
        "parameters": {
            "reference": "bound_baseline",
            "stage": "post_route",
            "metric_ids": ["critical_delay_ns", "slice_luts"],
        },
    })
    shutil.copy2(
        output / "requests" / "compare_metrics.json",
        request_root / "raw-legacy-conflict.json",
    )
    _write_request(
        request_root / "controlled-denied.json",
        "candidate_status",
        controlled_scope,
        {},
    )
    corrupt_request = json.loads((output / "requests" / "candidate_status.json").read_text())
    corrupt_request["scope"]["handle"]["store_id"] = "corrupt"
    dump(request_root / "corrupt-metadata.json", corrupt_request)
    shutil.copy2(
        output / "requests" / "read_artifact.json",
        request_root / "post-scan-budget.json",
    )

    corpus = json.loads((FIXTURE_ROOT / "corpus.json").read_text())
    source_root = Path(__file__).parents[1]
    for case in corpus["cases"]:
        for origin in case["oracle_origin"]:
            name = origin["artifact"]
            if name.startswith("generated:"):
                continue
            source = source_root / name
            destination = output / "oracle-sources" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    observed_hashes = {
        case["case_id"]: _sha256(output / case["request"]["path"])
        for case in corpus["cases"]
    }
    expected_hashes = {
        case["case_id"]: case["request"]["sha256"] for case in corpus["cases"]
    }
    if observed_hashes != expected_hashes:
        raise RuntimeError("generated acceptance requests do not match the frozen corpus")
    shutil.copy2(FIXTURE_ROOT / "corpus.json", output / "corpus.json")
    shutil.copy2(FIXTURE_ROOT / "ORACLE_REVIEW.json", output / "ORACLE_REVIEW.json")

    summary = {
        "schema_version": "chipcontext.acceptance-fixture.v1",
        "case_count": len(corpus["cases"]),
        "corpus_sha256": _sha256(output / "corpus.json"),
        "controlled_snapshot_ref": controlled.snapshot["content_hash"],
        "oracle_review_sha256": _sha256(output / "ORACLE_REVIEW.json"),
        "oracle_source_count": sum(
            not origin["artifact"].startswith("generated:")
            for case in corpus["cases"] for origin in case["oracle_origin"]
        ),
        "request_hashes": dict(sorted(observed_hashes.items())),
    }
    dump(output / "ACCEPTANCE_FIXTURE.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_acceptance_fixture(args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
