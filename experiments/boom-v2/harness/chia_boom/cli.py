from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ray

from chia.trace.profiler import get_collector, start_collector, stop_collector

from .artifacts import dump_json, load_json, sha256_file
from .campaign import campaign_report, init_campaign, run_campaign
from .core import validate_config
from .finalize import finalize_campaign, reconcile_interactive_finalization
from .environment import load_config
from .interactive import finalize_interactive_issueq, run_interactive_issueq
from .qualification import qualify


def connect(namespace: str, profile_dir: Path) -> None:
    ray.init(address="auto", namespace=namespace, ignore_reinit_error=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    start_collector(log_dir=str(profile_dir), namespace=namespace)


def save_profile(output: Path) -> None:
    actor = get_collector()
    if actor is not None:
        dump_json(output, ray.get(actor.get_events.remote()))


def require_qualification(config: dict, path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"qualification evidence is missing: {path}")
    evidence = load_json(path)
    if not evidence.get("passed"):
        raise RuntimeError("Q0/Q1 qualification did not pass")
    return evidence


def apply_qualification(campaign: Path, qualification_path: Path) -> None:
    manifest_path = campaign / "manifest.json"
    manifest = load_json(manifest_path)
    evidence = require_qualification(manifest["config"], qualification_path)
    qualified = int(evidence["qualified_parallel_runs"])
    manifest["config"]["search"]["parallel_runs"] = min(
        qualified, int(manifest["config"]["search"]["parallel_runs"])
    )
    manifest["qualification_sha256"] = sha256_file(qualification_path)
    manifest["qualified_parallel_runs"] = qualified
    dump_json(manifest_path, manifest)
    target = campaign / "frozen/QUALIFICATION.json"
    target.write_text(qualification_path.read_text())


def command_qualify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    output = args.output or Path(config["remote"]["install_root"]) / "qualification"
    connect("boom-v13-qualification", output / "profiler")
    try:
        result = qualify(config, output)
        save_profile(output / "CHIA_PROFILE.json")
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    finally:
        stop_collector()


def command_start(args: argparse.Namespace, *, preflight: bool) -> int:
    config = load_config(args.config)
    validate_config(config, formal=not preflight)
    qualification_path = Path(config["remote"]["install_root"]) / "qualification/QUALIFICATION.json"
    require_qualification(config, qualification_path)
    init_campaign(args.config, args.campaign, preflight=preflight, force=args.force)
    apply_qualification(args.campaign, qualification_path)
    connect(args.campaign.name, args.campaign / "profiler")
    try:
        results = run_campaign(args.campaign)
        save_profile(args.campaign / "CHIA_PROFILE.json")
        report = campaign_report(args.campaign)
        search_complete = all(item["status"] == "search_complete" for item in results)
        if preflight and not report.get("preflight", {}).get("passed", False):
            return 3
        return 0 if search_complete else 2
    finally:
        stop_collector()


def command_resume(args: argparse.Namespace) -> int:
    manifest = load_json(args.campaign / "manifest.json")
    connect(args.campaign.name, args.campaign / "profiler-resume")
    try:
        results = run_campaign(args.campaign)
        save_profile(args.campaign / "CHIA_PROFILE_RESUME.json")
        campaign_report(args.campaign)
        return 0 if all(item["status"] == "search_complete" for item in results) else 2
    finally:
        stop_collector()


def command_finalize(args: argparse.Namespace) -> int:
    connect(args.campaign.name + "-finalize", args.campaign / "profiler-finalize")
    try:
        results = finalize_campaign(args.campaign)
        save_profile(args.campaign / "CHIA_PROFILE_FINALIZE.json")
        campaign_report(args.campaign)
        return 0 if all(item["status"] in ("complete", "no_valid_candidate") for item in results) else 2
    finally:
        stop_collector()


def command_interactive(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    qualification_path = Path(config["remote"]["install_root"]) / "qualification/QUALIFICATION.json"
    require_qualification(config, qualification_path)
    connect(args.output.name, args.output / "profiler")
    try:
        result = run_interactive_issueq(
            config, args.output, seed=args.seed,
            max_turns=args.max_turns, max_evaluations=args.max_evaluations,
            memory_mode=args.memory_mode,
            auto_stop_improvement_percent=args.auto_stop_improvement_percent,
        )
        save_profile(args.output / "CHIA_PROFILE.json")
        print(json.dumps(result, indent=2))
        return 0 if result.get("best_candidate") else 3
    finally:
        stop_collector()


def command_interactive_finalize(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    connect(args.output.name + "-finalize", args.output / "profiler-finalize")
    try:
        result = finalize_interactive_issueq(config, args.output)
        save_profile(args.output / "CHIA_PROFILE_FINALIZE.json")
        print(json.dumps(result, indent=2))
        return 0 if result.get("final_valid") else 3
    finally:
        stop_collector()


def command_interactive_reconcile(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    result = reconcile_interactive_finalization(config, args.output)
    print(json.dumps(result, indent=2))
    return 0 if result.get("final_valid") else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chia_boom.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    qualify_parser = sub.add_parser("qualify")
    qualify_parser.add_argument("--config", type=Path, required=True)
    qualify_parser.add_argument("--output", type=Path)
    for name in ("preflight", "run"):
        item = sub.add_parser(name)
        item.add_argument("--config", type=Path, required=True)
        item.add_argument("--campaign", type=Path, required=True)
        item.add_argument("--force", action="store_true")
    for name in ("resume", "finalize", "report"):
        item = sub.add_parser(name)
        item.add_argument("--campaign", type=Path, required=True)
    interactive = sub.add_parser("interactive")
    interactive.add_argument("--config", type=Path, required=True)
    interactive.add_argument("--output", type=Path, required=True)
    interactive.add_argument("--seed", type=int, default=41)
    interactive.add_argument("--max-turns", type=int, default=24)
    interactive.add_argument("--max-evaluations", type=int, default=5)
    interactive.add_argument(
        "--memory-mode", choices=("none", "generic", "target"), default="none"
    )
    interactive.add_argument(
        "--auto-stop-improvement-percent", type=float, default=None,
        help="Stop search after a valid candidate reaches this post-synthesis delay improvement.",
    )
    interactive_finalize = sub.add_parser("interactive-finalize")
    interactive_finalize.add_argument("--config", type=Path, required=True)
    interactive_finalize.add_argument("--output", type=Path, required=True)
    interactive_reconcile = sub.add_parser("interactive-reconcile")
    interactive_reconcile.add_argument("--config", type=Path, required=True)
    interactive_reconcile.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "qualify":
        code = command_qualify(args)
    elif args.command == "preflight":
        code = command_start(args, preflight=True)
    elif args.command == "run":
        code = command_start(args, preflight=False)
    elif args.command == "resume":
        code = command_resume(args)
    elif args.command == "finalize":
        code = command_finalize(args)
    elif args.command == "interactive":
        code = command_interactive(args)
    elif args.command == "interactive-finalize":
        code = command_interactive_finalize(args)
    elif args.command == "interactive-reconcile":
        code = command_interactive_reconcile(args)
    else:
        print(json.dumps(campaign_report(args.campaign), indent=2))
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
