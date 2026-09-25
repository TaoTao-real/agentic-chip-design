from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .query import QueryError
from .query_cli import (
    DEFAULT_OUTPUT_BYTES,
    MAX_OUTPUT_BYTES,
    QueryAttemptFailure,
    run_query_attempt,
    write_attempt_audit,
)
from .schema import SchemaError
from .service import ChipContextService
from .store import DEFAULT_LIMIT_BYTES, EvidenceStore


def command_prepare(args: argparse.Namespace) -> int:
    request = json.loads(args.request.read_text())
    raw_root = request.get("input_root", ".")
    if not isinstance(raw_root, str) or Path(raw_root).is_absolute():
        raise SchemaError("input_root must be relative to the prepare request")
    input_root = (args.request.parent / raw_root).resolve(strict=True)
    store = EvidenceStore(args.output, {"input": input_root})
    result = ChipContextService(store).prepare(args.request.resolve())
    print(json.dumps(result.summary(), indent=2, sort_keys=True))
    return 0


def command_read(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.store)
    result = store.read_artifact(
        args.artifact,
        start_line=args.start_line,
        line_count=args.line_count,
        cursor=args.cursor,
        limit_bytes=args.limit_bytes,
        allowed_access={"public"} | ({"controlled"} if args.allow_controlled else set()),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_query(args: argparse.Namespace) -> int:
    try:
        result = run_query_attempt(
            args.registry.absolute(),
            args.request.absolute(),
            output_format=args.output_format,
            max_output_bytes=args.max_output_bytes,
            allow_controlled=args.allow_controlled,
        )
    except QueryAttemptFailure as exc:
        if args.audit_output is not None:
            write_attempt_audit(args.audit_output.absolute(), exc.audit)
        raise
    if args.audit_output is not None:
        write_attempt_audit(args.audit_output.absolute(), result.audit)
    sys.stdout.write(result.rendered)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chia-chipcontext")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--request", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    read = commands.add_parser("read-artifact")
    read.add_argument("--store", type=Path, required=True)
    read.add_argument("--artifact", required=True)
    read.add_argument("--start-line", type=int)
    read.add_argument("--line-count", type=int)
    read.add_argument("--cursor", type=int)
    read.add_argument("--limit-bytes", type=int, default=DEFAULT_LIMIT_BYTES)
    read.add_argument("--allow-controlled", action="store_true")
    query = commands.add_parser("query")
    query.add_argument("--registry", type=Path, required=True)
    query.add_argument("--request", type=Path, required=True)
    query.add_argument(
        "--format", dest="output_format", choices=("json", "markdown"),
        default="json",
    )
    query.add_argument(
        "--max-output-bytes", type=int, default=DEFAULT_OUTPUT_BYTES,
        help=f"final UTF-8 output budget (maximum {MAX_OUTPUT_BYTES})",
    )
    query.add_argument("--allow-controlled", action="store_true")
    query.add_argument(
        "--audit-output",
        type=Path,
        help=(
            "trusted new file for detailed success/rejection cost; the query "
            "request cannot select this sink"
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare":
            code = command_prepare(args)
        elif args.command == "read-artifact":
            code = command_read(args)
        else:
            code = command_query(args)
    except QueryAttemptFailure as exc:
        payload = json.dumps({
            "schema_version": "chipcontext.error.v1",
            "error": exc.code,
            "message": exc.public_message,
        }, indent=2, sort_keys=True)
        print(payload, file=sys.stderr)
        code = 2
    except (
        QueryError,
        SchemaError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        PermissionError,
        RuntimeError,
    ) as exc:
        if args.command != "query":
            error = type(exc).__name__
            message = str(exc)
        elif isinstance(exc, QueryError):
            error = exc.code
            message = str(exc)
        elif isinstance(exc, SchemaError):
            error = "invalid_request"
            message = str(exc)
        elif isinstance(exc, (RuntimeError, PermissionError)):
            error = "integrity_error"
            message = "query evidence failed validation"
        elif isinstance(exc, (FileNotFoundError, KeyError)):
            error = "unknown_reference"
            message = "query evidence is unavailable"
        else:
            error = type(exc).__name__
            message = str(exc)
        payload = json.dumps({
            "schema_version": "chipcontext.error.v1",
            "error": error,
            "message": message,
        }, indent=2, sort_keys=True)
        print(payload, file=sys.stderr if args.command == "query" else sys.stdout)
        code = 2
    except Exception:
        if args.command != "query":
            raise
        payload = json.dumps({
            "schema_version": "chipcontext.error.v1",
            "error": "internal_error",
            "message": "query attempt failed safely",
        }, indent=2, sort_keys=True)
        print(payload, file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
