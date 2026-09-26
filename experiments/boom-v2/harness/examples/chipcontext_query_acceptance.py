#!/usr/bin/env python3
"""Black-box ChipContext acceptance runner.

The verifier consumes literal, independently reviewed facts from a frozen
corpus.  It deliberately does not import production extractors or comparison
recipes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from chia_boom.chipcontext.query_cli import QueryAttemptFailure, run_query_attempt


CORPUS_SCHEMA = "chipcontext.acceptance-corpus.v1"
CASE_SCHEMA = "chipcontext.acceptance-case.v1"
ATTEMPT_SCHEMA = "chipcontext.acceptance-attempt.v1"
RESULT_SCHEMA = "chipcontext.acceptance-result.v1"
SUMMARY_SCHEMA = "chipcontext.acceptance-summary.v1"


class AcceptanceError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_below(root: Path, relative: Path, *, label: str) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise AcceptanceError(f"{label} escapes the trusted suite")
    cursor = root
    for component in relative.parts:
        if component in {"", "."}:
            continue
        cursor = cursor / component
        if cursor.is_symlink():
            raise AcceptanceError(f"{label} may not traverse a symlink")
    resolved = cursor.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        raise AcceptanceError(f"{label} escapes the trusted suite")
    return resolved


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def strict_object(
    value: Any, *, name: str, required: set[str], optional: set[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AcceptanceError(f"{name} must be an object")
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise AcceptanceError(f"{name} contains unknown fields: {sorted(unknown)}")
    if missing:
        raise AcceptanceError(f"{name} is missing fields: {sorted(missing)}")
    return value


def load_corpus(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("corpus is unavailable or invalid") from exc
    raw = strict_object(
        value,
        name="corpus",
        required={
            "schema_version",
            "corpus_id",
            "corpus_revision",
            "planned_case_count",
            "implemented_case_count",
            "oracle_review",
            "execution_limits",
            "cases",
        },
    )
    if raw["schema_version"] != CORPUS_SCHEMA:
        raise AcceptanceError("corpus schema is unsupported")
    if not isinstance(raw["cases"], list) or not raw["cases"]:
        raise AcceptanceError("corpus cases must be a non-empty array")
    if raw["implemented_case_count"] != len(raw["cases"]):
        raise AcceptanceError("implemented case count does not match corpus")
    case_ids: set[str] = set()
    for case in raw["cases"]:
        parsed = strict_object(
            case,
            name="case",
            required={
                "schema_version",
                "case_id",
                "question",
                "requirement",
                "category",
                "source_type",
                "request",
                "execution",
                "expected",
                "oracle_origin",
            },
        )
        if parsed["schema_version"] != CASE_SCHEMA:
            raise AcceptanceError("case schema is unsupported")
        case_id = parsed["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise AcceptanceError("case IDs must be unique non-empty strings")
        case_ids.add(case_id)
        if parsed["source_type"] not in {"synthetic", "redacted", "controlled_real"}:
            raise AcceptanceError("case source type is unsupported")
    return raw


def load_review(corpus_path: Path, corpus: dict[str, Any]) -> dict[str, Any]:
    review_spec = strict_object(
        corpus["oracle_review"],
        name="oracle_review",
        required={"required", "record"},
    )
    record_name = review_spec["record"]
    if not isinstance(record_name, str) or Path(record_name).name != record_name:
        raise AcceptanceError("oracle review must be a sibling file")
    try:
        review = json.loads((corpus_path.parent / record_name).read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("oracle review record is unavailable or invalid") from exc
    review = strict_object(
        review,
        name="oracle review record",
        required={
            "schema_version",
            "corpus_id",
            "corpus_sha256",
            "status",
            "reviewer_role",
            "review_method",
            "approved_by",
            "approved_at",
        },
    )
    if review["schema_version"] != "chipcontext.oracle-review.v1":
        raise AcceptanceError("oracle review schema is unsupported")
    if review["corpus_id"] != corpus["corpus_id"]:
        raise AcceptanceError("oracle review references another corpus")
    if review["corpus_sha256"] != sha256_path(corpus_path):
        raise AcceptanceError("oracle review corpus hash is stale")
    if review["status"] not in {"pending", "approved", "rejected"}:
        raise AcceptanceError("oracle review status is invalid")
    if review["status"] == "approved" and (
        not review["approved_by"] or not review["approved_at"]
    ):
        raise AcceptanceError("approved oracle review lacks reviewer evidence")
    return review


def resolve_request(registry: Path, case: dict[str, Any]) -> Path:
    request_spec = strict_object(
        case["request"],
        name="case request",
        required={"path", "sha256"},
    )
    root = registry.parent.resolve(strict=True)
    request = resolve_below(
        root, Path(request_spec["path"]), label="case request"
    )
    if not request.is_file():
        raise AcceptanceError("case request is unavailable")
    if sha256_path(request) != request_spec["sha256"]:
        raise AcceptanceError("case request hash does not match the frozen corpus")
    return request


def validate_oracle_origins(
    registry: Path, corpus: dict[str, Any]
) -> dict[str, int]:
    suite_root = registry.parent.resolve(strict=True)
    origin_count = 0
    span_count = 0
    for case in corpus["cases"]:
        origins = case["oracle_origin"]
        if not isinstance(origins, list) or not origins:
            raise AcceptanceError("each case requires at least one oracle origin")
        for value in origins:
            origin = strict_object(
                value,
                name="oracle origin",
                required={"artifact", "sha256", "manual_method"},
                optional={"json_pointer", "line_span"},
            )
            name = origin["artifact"]
            if not isinstance(name, str) or not name:
                raise AcceptanceError("oracle origin artifact is invalid")
            if name.startswith("generated:"):
                relative = Path(name.removeprefix("generated:"))
            else:
                relative = Path("oracle-sources") / name
            artifact = resolve_below(
                suite_root, relative, label="oracle origin"
            )
            if not artifact.is_file():
                raise AcceptanceError("oracle origin is unavailable")
            if sha256_path(artifact) != origin["sha256"]:
                raise AcceptanceError("oracle origin hash does not match raw evidence")
            if "json_pointer" in origin:
                try:
                    document = json.loads(artifact.read_text())
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise AcceptanceError("oracle JSON origin is invalid") from exc
                found, _ = pointer_get(document, origin["json_pointer"])
                if not found:
                    raise AcceptanceError("oracle JSON pointer is outside raw evidence")
                span_count += 1
            if "line_span" in origin:
                line_span = strict_object(
                    origin["line_span"],
                    name="oracle line span",
                    required={"start", "end"},
                )
                line_count = len(artifact.read_text().splitlines())
                if not (
                    isinstance(line_span["start"], int)
                    and isinstance(line_span["end"], int)
                    and 1 <= line_span["start"] <= line_span["end"] <= line_count
                ):
                    raise AcceptanceError("oracle line span is outside raw evidence")
                span_count += 1
            origin_count += 1
    return {"origin_count": origin_count, "located_span_count": span_count}


def pointer_get(value: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise AcceptanceError("assertion pointer is invalid")
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def assertion_matches(assertion: dict[str, Any], targets: dict[str, Any]) -> tuple[bool, str]:
    assertion = strict_object(
        assertion,
        name="assertion",
        required={"target", "pointer", "operator"},
        optional={"value"},
    )
    target_name = assertion["target"]
    if target_name not in {"answer", "error", "audit"}:
        raise AcceptanceError("assertion target is unsupported")
    exists, actual = pointer_get(targets.get(target_name), assertion["pointer"])
    operator = assertion["operator"]
    expected = assertion.get("value")
    matched = False
    if operator == "exists":
        matched = exists
    elif operator == "equals":
        matched = exists and actual == expected
    elif operator == "is_null":
        matched = exists and (actual is None) == expected
    elif operator == "length":
        matched = exists and hasattr(actual, "__len__") and len(actual) == expected
    elif operator == "min_length":
        matched = exists and hasattr(actual, "__len__") and len(actual) >= expected
    elif operator == "gte":
        matched = (
            exists
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and actual >= expected
        )
    else:
        raise AcceptanceError(f"assertion operator is unsupported: {operator}")
    detail = json.dumps(
        {
            "target": target_name,
            "pointer": assertion["pointer"],
            "operator": operator,
            "expected": expected,
            "exists": exists,
            "actual": actual,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return matched, detail


def verify_case(
    case: dict[str, Any], *, query_status: str, targets: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    expected = strict_object(
        case["expected"],
        name="case expected",
        required={"outcome", "assertions", "forbidden"},
    )
    outcome = expected["outcome"]
    if outcome not in {"success", "unknown", "rejection"}:
        raise AcceptanceError("expected outcome is unsupported")
    expected_status = "rejected" if outcome == "rejection" else "success"
    records = [{
        "kind": "query_status",
        "passed": query_status == expected_status,
        "detail": f"expected {expected_status}, observed {query_status}",
    }]
    for assertion in expected["assertions"]:
        matched, detail = assertion_matches(assertion, targets)
        records.append({"kind": "required", "passed": matched, "detail": detail})
    for assertion in expected["forbidden"]:
        matched, detail = assertion_matches(assertion, targets)
        records.append({"kind": "forbidden", "passed": not matched, "detail": detail})
    return all(record["passed"] for record in records), records


def run_api(
    registry: Path, request: Path, execution: dict[str, Any], output_format: str
) -> dict[str, Any]:
    started = time.monotonic_ns()
    try:
        result = run_query_attempt(
            registry,
            request,
            output_format=output_format,
            max_output_bytes=execution["max_output_bytes"],
            allow_controlled=execution["allow_controlled"],
        )
    except QueryAttemptFailure as exc:
        return {
            "query_status": "rejected",
            "response": None,
            "error": {"error": exc.code, "message": exc.public_message},
            "audit": exc.audit,
            "outer_wall_time_ns": time.monotonic_ns() - started,
        }
    response = json.loads(result.rendered) if output_format == "json" else None
    return {
        "query_status": "success",
        "response": response,
        "error": None,
        "audit": result.audit,
        "rendered_sha256": hashlib.sha256(result.rendered.encode()).hexdigest(),
        "outer_wall_time_ns": time.monotonic_ns() - started,
    }


def run_cli(
    registry: Path,
    request: Path,
    execution: dict[str, Any],
    output_format: str,
    audit_path: Path,
    timeout: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "chia_boom.chipcontext.cli",
        "query",
        "--registry",
        str(registry),
        "--request",
        str(request),
        "--format",
        output_format,
        "--max-output-bytes",
        str(execution["max_output_bytes"]),
        "--audit-output",
        str(audit_path),
    ]
    if execution["allow_controlled"]:
        command.append("--allow-controlled")
    started = time.monotonic_ns()
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "query_status": "runner_error",
            "runner_error": "timeout",
            "response": None,
            "error": None,
            "audit": None,
            "cost_unknown_reason": "query process timed out before trusted audit",
            "outer_wall_time_ns": time.monotonic_ns() - started,
        }
    audit = None
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text())
        except (UnicodeError, json.JSONDecodeError):
            pass
    if completed.returncode == 0:
        status = "success"
    elif completed.returncode == 2:
        status = "rejected"
    else:
        status = "runner_error"
    response = None
    error = None
    try:
        if status == "success" and output_format == "json":
            response = json.loads(completed.stdout)
        elif status == "rejected":
            error = json.loads(completed.stderr)
    except json.JSONDecodeError:
        status = "runner_error"
    result = {
        "query_status": status,
        "exit_code": completed.returncode,
        "response": response,
        "error": error,
        "audit": audit,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "outer_wall_time_ns": time.monotonic_ns() - started,
    }
    if status == "runner_error":
        result["runner_error"] = (
            "CLI output was invalid" if completed.returncode in {0, 2}
            else f"CLI exited with unexpected code {completed.returncode}"
        )
        if audit is None:
            result["cost_unknown_reason"] = (
                "query output could not be associated with a trusted audit"
            )
    elif audit is None:
        result["query_status"] = "runner_error"
        result["runner_error"] = "trusted attempt audit is missing or invalid"
        result["cost_unknown_reason"] = "trusted attempt audit unavailable"
    return result


def targets_from_execution(execution: dict[str, Any]) -> dict[str, Any]:
    response = execution.get("response")
    return {
        "answer": response.get("answer") if isinstance(response, dict) else None,
        "error": execution.get("error"),
        "audit": execution.get("audit"),
    }


def case_result(
    case: dict[str, Any], attempts: list[dict[str, Any]]
) -> dict[str, Any]:
    passed = bool(attempts) and all(attempt["case_passed"] for attempt in attempts)
    blocked = any(
        attempt["query_status"] == "runner_error" for attempt in attempts
    )
    status = "blocked" if blocked else ("pass" if passed else "fail")
    rejection_count = sum(
        attempt["query_status"] == "rejected" for attempt in attempts
    )
    return {
        "schema_version": RESULT_SCHEMA,
        "case_id": case["case_id"],
        "category": case["category"],
        "expected_outcome": case["expected"]["outcome"],
        "status": status,
        "attempt_ids": [attempt["attempt_id"] for attempt in attempts],
        "attempt_count": len(attempts),
        "rejection_count": rejection_count,
        "source_origin_count": len(case["oracle_origin"]),
        "oracle_origin_refs": [
            {
                key: origin[key]
                for key in ("artifact", "sha256", "json_pointer", "line_span")
                if key in origin
            }
            for origin in case["oracle_origin"]
        ],
    }


def build_summary(
    run_id: str,
    corpus: dict[str, Any],
    review: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "blocked": 0}
    expected_rejection_cases = 0
    expected_rejection_attempts = 0
    unexpected_rejection_attempts = 0
    categories: dict[str, dict[str, int]] = {}
    for result in results:
        counts[result["status"]] += 1
        row = categories.setdefault(
            result["category"], {"pass": 0, "fail": 0, "blocked": 0}
        )
        row[result["status"]] += 1
        if result["expected_outcome"] == "rejection":
            expected_rejection_cases += 1
            expected_rejection_attempts += result["rejection_count"]
        else:
            unexpected_rejection_attempts += result["rejection_count"]
    formal_eligible = (
        review["status"] == "approved"
        and counts["fail"] == 0
        and counts["blocked"] == 0
    )
    return {
        "schema_version": SUMMARY_SCHEMA,
        "run_id": run_id,
        "corpus_id": corpus["corpus_id"],
        "corpus_revision": corpus["corpus_revision"],
        "oracle_review_status": review["status"],
        "formal_eligible": formal_eligible,
        "counts": counts,
        "expected_rejection_cases": expected_rejection_cases,
        "expected_rejection_attempts": expected_rejection_attempts,
        "unexpected_rejection_attempts": unexpected_rejection_attempts,
        "categories": dict(sorted(categories.items())),
        "source_origin_count": sum(row["source_origin_count"] for row in results),
    }


def render_report(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        "# ChipContext acceptance run",
        "",
        f"- Run: `{summary['run_id']}`",
        f"- Corpus: `{summary['corpus_id']}` revision `{summary['corpus_revision']}`",
        f"- Oracle review: `{summary['oracle_review_status']}`",
        f"- Formal evidence eligible: `{str(summary['formal_eligible']).lower()}`",
        (
            f"- Results: {summary['counts']['pass']} pass, "
            f"{summary['counts']['fail']} fail, "
            f"{summary['counts']['blocked']} blocked"
        ),
        "",
        "| Case | Category | Expected | Result |",
        "|---|---|---|---|",
    ]
    for result in sorted(results, key=lambda row: row["case_id"]):
        lines.append(
            f"| `{result['case_id']}` | `{result['category']}` | "
            f"`{result['expected_outcome']}` | `{result['status']}` |"
        )
    lines.extend([
        "",
        (
            "> A passing development run is not formal acceptance until the "
            "independent oracle review is approved."
        ),
        "",
    ])
    return "\n".join(lines)


def run_acceptance(
    *,
    corpus_path: Path,
    registry: Path,
    mode: str,
    output: Path,
    require_approved_oracle: bool,
    code_version: str,
) -> dict[str, Any]:
    corpus_path = corpus_path.resolve(strict=True)
    registry = registry.resolve(strict=True)
    corpus = load_corpus(corpus_path)
    review = load_review(corpus_path, corpus)
    origin_validation = validate_oracle_origins(registry, corpus)
    requests = {
        case["case_id"]: resolve_request(registry, case) for case in corpus["cases"]
    }
    if require_approved_oracle and review["status"] != "approved":
        raise AcceptanceError("formal acceptance requires an approved oracle review")
    if output.exists():
        raise AcceptanceError("output directory already exists")
    output.mkdir(parents=True, mode=0o700)
    (output / "attempts").mkdir()
    (output / "case-results").mkdir()
    run_id = f"cc02c-{time.time_ns()}-{uuid.uuid4().hex[:12]}"
    manifest = {
        "schema_version": "chipcontext.acceptance-run.v1",
        "run_id": run_id,
        "corpus_id": corpus["corpus_id"],
        "corpus_revision": corpus["corpus_revision"],
        "corpus_sha256": sha256_path(corpus_path),
        "oracle_review_sha256": sha256_path(corpus_path.parent / corpus["oracle_review"]["record"]),
        "oracle_review_status": review["status"],
        "registry_sha256": sha256_path(registry),
        "mode": mode,
        "code_version": code_version,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "model_calls": 0,
        "new_eda_or_simulator_calls": 0,
        "oracle_origin_validation": origin_validation,
    }
    write_json(output / "run-manifest.json", manifest)
    event_path = output / "events.jsonl"
    results: list[dict[str, Any]] = []
    runner_kinds = ["api", "cli"] if mode == "both" else [mode]
    limits = corpus["execution_limits"]
    for case in corpus["cases"]:
        request = requests[case["case_id"]]
        execution = strict_object(
            case["execution"],
            name="case execution",
            required={"allow_controlled", "formats", "max_output_bytes"},
        )
        attempts: list[dict[str, Any]] = []
        for runner_kind in runner_kinds:
            for output_format in execution["formats"]:
                attempt_id = uuid.uuid4().hex
                audit_path = output / "attempts" / f"{case['case_id']}-{attempt_id}.audit.json"
                if runner_kind == "api":
                    observed = run_api(registry, request, execution, output_format)
                else:
                    observed = run_cli(
                        registry,
                        request,
                        execution,
                        output_format,
                        audit_path,
                        int(limits["attempt_timeout_seconds"]),
                    )
                passed, assertions = verify_case(
                    case,
                    query_status=observed["query_status"],
                    targets=targets_from_execution(observed),
                )
                attempt = {
                    "schema_version": ATTEMPT_SCHEMA,
                    "run_id": run_id,
                    "case_id": case["case_id"],
                    "attempt_id": attempt_id,
                    "runner": runner_kind,
                    "format": output_format,
                    "request_sha256": sha256_path(request),
                    "query_status": observed["query_status"],
                    "case_passed": passed,
                    "assertions": assertions,
                    "observed": observed,
                }
                attempt_file = output / "attempts" / f"{case['case_id']}-{attempt_id}.json"
                write_json(attempt_file, attempt)
                event = {
                    "schema_version": "chipcontext.acceptance-event.v1",
                    "run_id": run_id,
                    "case_id": case["case_id"],
                    "attempt_id": attempt_id,
                    "event": "attempt_completed",
                    "query_status": observed["query_status"],
                    "case_passed": passed,
                    "attempt_sha256": sha256_path(attempt_file),
                }
                with event_path.open("a", encoding="utf-8") as handle:
                    handle.write(canonical_bytes(event).decode("utf-8") + "\n")
                attempts.append(attempt)
        result = case_result(case, attempts)
        write_json(output / "case-results" / f"{case['case_id']}.json", result)
        results.append(result)
    summary = build_summary(run_id, corpus, review, results)
    write_json(output / "summary.json", summary)
    (output / "report.md").write_text(render_report(summary, results))
    return summary


def rebuild_summary_from_run(run: Path) -> dict[str, Any]:
    """Recompute summary facts from immutable per-case results."""
    manifest = json.loads((run / "run-manifest.json").read_text())
    results = [
        json.loads(path.read_text())
        for path in sorted((run / "case-results").glob("*.json"))
    ]
    corpus_stub = {
        "corpus_id": manifest["corpus_id"],
        "corpus_revision": manifest["corpus_revision"],
    }
    review_stub = {"status": manifest["oracle_review_status"]}
    return build_summary(manifest["run_id"], corpus_stub, review_stub, results)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--mode", choices=("api", "cli", "both"), default="both")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-approved-oracle", action="store_true")
    parser.add_argument(
        "--code-version",
        default=os.environ.get("CHIPCONTEXT_CODE_VERSION", "unknown"),
    )
    args = parser.parse_args()
    try:
        summary = run_acceptance(
            corpus_path=args.corpus,
            registry=args.registry,
            mode=args.mode,
            output=args.output,
            require_approved_oracle=args.require_approved_oracle,
            code_version=args.code_version,
        )
    except AcceptanceError as exc:
        print(
            json.dumps({
                "error": "acceptance_configuration_error",
                "message": str(exc),
            }),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["counts"]["fail"] or summary["counts"]["blocked"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
