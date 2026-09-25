from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        code = command_prepare(args) if args.command == "prepare" else command_read(args)
    except (
        SchemaError,
        FileNotFoundError,
        json.JSONDecodeError,
        KeyError,
        PermissionError,
        RuntimeError,
    ) as exc:
        print(json.dumps({
            "schema_version": "chipcontext.error.v1",
            "error": type(exc).__name__,
            "message": str(exc),
        }, indent=2, sort_keys=True))
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
