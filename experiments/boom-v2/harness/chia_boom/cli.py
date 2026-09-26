from __future__ import annotations

import argparse
import json
from pathlib import Path

import ray

from chia.trace.profiler import get_collector, start_collector, stop_collector

from .artifacts import dump_json, load_json, sha256_file
from .campaign import campaign_report, init_campaign, run_campaign
from .cc03t import (
    prepare_experiment,
    report_experiment,
    resume_experiment,
    run_experiment,
)
from .core import validate_config
from .deployment import doctor
from .environment import load_config
from .finalize import finalize_campaign, reconcile_interactive_finalization
from .frozen import seal_frozen_run, verify_frozen_run, verify_qualification
from .interactive import (
    finalize_interactive_issueq,
    resume_interactive_issueq,
    run_interactive_issueq,
)
from .information_audit import AuditError, audit_campaign, audit_campaign_pair
from .qualification import qualify
from .smoke import run_baseline_smoke
from .optimization_trace import trace_build_cost


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
    verify_qualification(config, evidence)
    return evidence


def apply_qualification(campaign: Path, qualification_path: Path) -> None:
    manifest_path = campaign / "manifest.json"
    manifest = load_json(manifest_path)
    evidence = require_qualification(manifest["config"], qualification_path)
    qualified = int(evidence["qualified_parallel_runs"])
    manifest["effective_parallel_runs"] = min(
        qualified, int(manifest["config"]["search"]["parallel_runs"])
    )
    manifest["qualification_sha256"] = sha256_file(qualification_path)
    manifest["qualified_parallel_runs"] = qualified
    dump_json(manifest_path, manifest)
    target = campaign / "frozen/QUALIFICATION.json"
    target.write_text(qualification_path.read_text())
    frozen = seal_frozen_run(campaign / "frozen", manifest["config"])
    manifest["frozen_run_fingerprint"] = frozen["fingerprint"]
    dump_json(manifest_path, manifest)


def require_campaign_frozen(campaign: Path, manifest: dict) -> None:
    frozen_root = campaign / "frozen"
    verify_frozen_run(frozen_root, manifest.get("frozen_run_fingerprint"))
    require_qualification(manifest["config"], frozen_root / "QUALIFICATION.json")


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


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = doctor(
        config,
        require_qualification=args.require_qualification,
        check_api=args.check_api,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 2


def command_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    result = run_baseline_smoke(
        config,
        config_path=args.config,
        output=args.output,
        target_id=args.target,
        cycles=args.cycles,
        seed=args.seed,
        jobs=args.jobs,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 3


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
    require_campaign_frozen(args.campaign, manifest)
    connect(args.campaign.name, args.campaign / "profiler-resume")
    try:
        results = run_campaign(args.campaign)
        save_profile(args.campaign / "CHIA_PROFILE_RESUME.json")
        campaign_report(args.campaign)
        return 0 if all(item["status"] == "search_complete" for item in results) else 2
    finally:
        stop_collector()


def command_finalize(args: argparse.Namespace) -> int:
    manifest = load_json(args.campaign / "manifest.json")
    require_campaign_frozen(args.campaign, manifest)
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
            feedback_arm=args.feedback_arm,
            auto_stop_improvement_percent=args.auto_stop_improvement_percent,
        )
        save_profile(args.output / "CHIA_PROFILE.json")
        print(json.dumps(result, indent=2))
        return 0 if result.get("best_candidate") else 3
    finally:
        stop_collector()


def command_interactive_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    validate_config(config)
    connect(args.output.name, args.output / "profiler-resume")
    try:
        result = resume_interactive_issueq(config, args.output)
        save_profile(args.output / "CHIA_PROFILE_RESUME.json")
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


def command_audit_information(args: argparse.Namespace) -> int:
    try:
        result = audit_campaign(args.campaign, args.output)
    except AuditError as exc:
        print(json.dumps({"status": "audit_error", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


def command_audit_information_pair(args: argparse.Namespace) -> int:
    try:
        result = audit_campaign_pair(args.e0, args.e1, args.output)
    except AuditError as exc:
        print(json.dumps({"status": "audit_error", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0


def command_trace_build(args: argparse.Namespace) -> int:
    result = trace_build_cost(args.campaign, args.output)
    print(json.dumps(result, indent=2))
    return 0


def command_cc03t_prepare(args: argparse.Namespace) -> int:
    result = prepare_experiment(args.config, args.manifest, args.output)
    print(json.dumps(result, indent=2))
    return 0


def command_cc03t_run(args: argparse.Namespace, *, resume: bool = False) -> int:
    config = load_config(args.output / "CONFIG.json")
    validate_config(config)
    qualification_path = Path(config["remote"]["install_root"]) / "qualification/QUALIFICATION.json"
    require_qualification(config, qualification_path)
    connect(args.output.name + ("-resume" if resume else ""), args.output / ("profiler-resume" if resume else "profiler"))
    try:
        result = resume_experiment(args.output) if resume else run_experiment(args.output)
        save_profile(args.output / ("CHIA_PROFILE_RESUME.json" if resume else "CHIA_PROFILE.json"))
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") == "complete" else 3
    finally:
        stop_collector()


def command_cc03t_report(args: argparse.Namespace) -> int:
    result = report_experiment(args.output)
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chia_boom.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    qualify_parser = sub.add_parser("qualify")
    qualify_parser.add_argument("--config", type=Path, required=True)
    qualify_parser.add_argument("--output", type=Path)
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--config", type=Path, required=True)
    doctor_parser.add_argument("--require-qualification", action="store_true")
    doctor_parser.add_argument("--check-api", action="store_true")
    smoke_parser = sub.add_parser("smoke")
    smoke_parser.add_argument("--config", type=Path, required=True)
    smoke_parser.add_argument("--output", type=Path, required=True)
    smoke_parser.add_argument("--target")
    smoke_parser.add_argument("--cycles", type=int, default=10_000)
    smoke_parser.add_argument("--seed", type=int, default=20_260_924)
    smoke_parser.add_argument("--jobs", type=int)
    smoke_parser.add_argument("--force", action="store_true")
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
        "--feedback-arm", choices=("E0", "E1"), default="E0",
        help="E0 exposes raw candidate artifacts; E1 adds structured ChipContext feedback.",
    )
    interactive.add_argument(
        "--auto-stop-improvement-percent", type=float, default=None,
        help="Stop search after a valid candidate reaches this post-synthesis delay improvement.",
    )
    interactive_finalize = sub.add_parser("interactive-finalize")
    interactive_finalize.add_argument("--config", type=Path, required=True)
    interactive_finalize.add_argument("--output", type=Path, required=True)
    interactive_resume = sub.add_parser("interactive-resume")
    interactive_resume.add_argument("--config", type=Path, required=True)
    interactive_resume.add_argument("--output", type=Path, required=True)
    interactive_reconcile = sub.add_parser("interactive-reconcile")
    interactive_reconcile.add_argument("--config", type=Path, required=True)
    interactive_reconcile.add_argument("--output", type=Path, required=True)
    audit = sub.add_parser("audit-information")
    audit.add_argument("--campaign", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit_pair = sub.add_parser("audit-information-pair")
    audit_pair.add_argument("--e0", type=Path, required=True)
    audit_pair.add_argument("--e1", type=Path, required=True)
    audit_pair.add_argument("--output", type=Path, required=True)
    trace_build = sub.add_parser("trace-build")
    trace_build.add_argument("--campaign", type=Path, required=True)
    trace_build.add_argument("--output", type=Path, required=True)
    cc03t_prepare = sub.add_parser("cc03t-prepare")
    cc03t_prepare.add_argument("--config", type=Path, required=True)
    cc03t_prepare.add_argument("--manifest", type=Path, required=True)
    cc03t_prepare.add_argument("--output", type=Path, required=True)
    for name in ("cc03t-run", "cc03t-resume", "cc03t-report"):
        item = sub.add_parser(name)
        item.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "doctor":
        code = command_doctor(args)
    elif args.command == "smoke":
        code = command_smoke(args)
    elif args.command == "qualify":
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
    elif args.command == "interactive-resume":
        code = command_interactive_resume(args)
    elif args.command == "interactive-finalize":
        code = command_interactive_finalize(args)
    elif args.command == "interactive-reconcile":
        code = command_interactive_reconcile(args)
    elif args.command == "audit-information":
        code = command_audit_information(args)
    elif args.command == "audit-information-pair":
        code = command_audit_information_pair(args)
    elif args.command == "trace-build":
        code = command_trace_build(args)
    elif args.command == "cc03t-prepare":
        code = command_cc03t_prepare(args)
    elif args.command == "cc03t-run":
        code = command_cc03t_run(args)
    elif args.command == "cc03t-resume":
        code = command_cc03t_run(args, resume=True)
    elif args.command == "cc03t-report":
        code = command_cc03t_report(args)
    else:
        print(json.dumps(campaign_report(args.campaign), indent=2))
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
