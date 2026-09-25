#!/usr/bin/env python3
"""Public-safe CC-02b foundation example over a prepare request."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chia_boom.chipcontext import (
    CandidateRef,
    ChipContextQueryService,
    ChipContextService,
    EvidenceHandle,
    EvidenceStore,
    QueryScope,
)
from chia_boom.chipcontext.schema import SchemaError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    request_path = args.request.resolve(strict=True)
    request = json.loads(request_path.read_text())
    input_root = request.get("input_root", ".")
    if not isinstance(input_root, str) or Path(input_root).is_absolute():
        raise SchemaError("input_root must be relative to the prepare request")
    artifact_root = (request_path.parent / input_root).resolve(strict=True)

    store = EvidenceStore(args.output, {"input": artifact_root})
    prepared = ChipContextService(store).prepare(request_path)
    candidate = CandidateRef(**{
        key: value
        for key, value in prepared.snapshot["candidate_ref"].items()
        if key != "ref_id"
    })
    scope = QueryScope(
        EvidenceHandle("example", prepared.snapshot["content_hash"]),
        expected_candidate=candidate,
        expected_attempt_id=prepared.evaluation_manifest["attempt_id"],
        working_source_sha256=candidate.source_sha256,
    )
    query = ChipContextQueryService(
        {"example": store}, {"example": {"public"}}
    )
    status = query.get_candidate_status(scope)
    artifacts = query.list_candidate_artifacts(scope, limit=100)
    selected = artifacts["result"]["artifacts"][0]
    source = query.read_artifact(
        scope, selected["content_ref"], start_line=1, line_count=4
    )
    print(json.dumps({
        "schema_version": "chipcontext.query-foundation-example.v1",
        "status": status,
        "artifacts": artifacts,
        "source": source,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
