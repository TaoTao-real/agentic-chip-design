from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from chia.base.ChiaFunction import ChiaFunction
from chia.chipyard.chisel_build_node import ChiselBuildNode
from chia.chipyard.state_def import BuildTarget
from chia.chipyard.verilator_run_node import VerilatorRunNode

from .artifacts import (
    CandidateArtifact,
    EvaluationArtifact,
    StageResult,
    dump_json,
    load_json,
    sha256_file,
)
from .core import canonical_hash, search_evaluation_data, update_validity
from .environment import chipyard_environment_command
from .frozen import (
    immutable_contract,
    load_frozen_run_config,
    qualification_request,
    qualified_artifact_hashes,
    regression_binary,
    verify_frozen_run,
)
from .nodes import (
    BoomDifferentialNode,
    BoomElaborationNode,
    BoomVivadoNode,
    acquire_workspace,
)


def _activate_chipyard_environment(
    chipyard: Path, config: dict[str, Any]
) -> None:
    command = chipyard_environment_command(config, chipyard) + "; env -0"
    result = subprocess.run(
        ["bash", "-lc", command], capture_output=True, timeout=300
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-8000:])
    for entry in result.stdout.split(b"\0"):
        if b"=" in entry:
            key, value = entry.split(b"=", 1)
            os.environ[key.decode(errors="replace")] = value.decode(errors="replace")


def _verilator_run_passed(run: Any) -> bool:
    """Use CHIA's VerilatorRunNode contract: exit status zero is success.

    Chipyard's bare-metal ``rsort.riscv`` reaches TestDriver ``$finish`` and
    exits zero, but does not print a ``*** PASSED ***`` banner.  Requiring that
    unrelated marker incorrectly rejects both the frozen baseline and valid
    candidates after a successful full-processor execution.
    """
    return bool(run.success and run.returncode == 0)


def build_finalization_identity(
    candidate: CandidateArtifact,
    target: dict[str, Any],
    config: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    frozen_root = config.get("frozen_run_root")
    if frozen_root:
        frozen_manifest = load_json(Path(frozen_root) / "FROZEN_RUN_MANIFEST.json")
        reference = {"frozen_run_fingerprint": frozen_manifest["fingerprint"]}
    else:
        request = qualification_request(config)
        reference = {
            "qualification_fingerprint": request["fingerprint"],
            "qualified_artifact_hashes": qualified_artifact_hashes(config),
        }
    value = {
        "schema_version": "finalization-identity-v1",
        "candidate_id": candidate.id,
        "candidate_source_sha256": hashlib.sha256(candidate.source.encode()).hexdigest(),
        "target": target,
        "contract": immutable_contract(config),
        "baseline": baseline,
        "differential_seeds": [42, 43, 44],
        "regression_binary_sha256": sha256_file(regression_binary(config)),
        "reference": reference,
    }
    return value | {"fingerprint": canonical_hash(value)}


def _cached_finalization(
    root: Path,
    identity: dict[str, Any],
) -> tuple[EvaluationArtifact | None, int, list[dict[str, Any]]]:
    paths = []
    legacy = root / "evaluation.json"
    if legacy.exists():
        paths.append(legacy)
    paths.extend(sorted(root.glob("attempt-*/evaluation.json")))
    rejections = []
    for path in paths:
        evaluation = EvaluationArtifact.from_dict(load_json(path))
        identity_path = path.parent / "IDENTITY.json"
        stored = load_json(identity_path) if identity_path.exists() else {}
        reasons = []
        if stored.get("fingerprint") != identity["fingerprint"]:
            reasons.append("identity_mismatch")
        if evaluation.candidate_id != identity["candidate_id"]:
            reasons.append("candidate_mismatch")
        if evaluation.hashes.get("candidate_source_sha256") != identity["candidate_source_sha256"]:
            reasons.append("source_mismatch")
        if reasons:
            rejections.append({"path": str(path), "reasons": reasons})
            continue
        if evaluation.status == "infra_blocked" and evaluation.retryable:
            continue
        return evaluation, len(list(root.glob("attempt-*"))) + 1, rejections
    return None, len(list(root.glob("attempt-*"))) + 1, rejections


def _finalization_attempts(
    root: Path, identity: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return every attempt for this exact finalization contract."""
    attempts = []
    for path in sorted(root.glob("attempt-*/evaluation.json")):
        identity_path = path.parent / "IDENTITY.json"
        if not identity_path.exists():
            continue
        stored = load_json(identity_path)
        if stored.get("fingerprint") != identity["fingerprint"]:
            continue
        evaluation = EvaluationArtifact.from_dict(load_json(path))
        metadata_path = path.parent / "METADATA.json"
        metadata = load_json(metadata_path) if metadata_path.exists() else {}
        attempts.append({
            "attempt": int(path.parent.name.split("-")[-1]),
            "path": str(path),
            "status": evaluation.status,
            "retryable": evaluation.retryable,
            "failure_class": evaluation.failure_class,
            "final_valid": evaluation.final_valid,
            "active_seconds": evaluation.active_seconds,
            "completed_epoch": metadata.get("completed_epoch"),
        })
    return attempts


def finalize_candidate(
    *,
    node: "BoomFinalizationNode",
    candidate: CandidateArtifact,
    early: EvaluationArtifact,
    target: dict[str, Any],
    config: dict[str, Any],
    baseline: dict[str, Any],
    root: Path,
    display_name: str,
) -> tuple[EvaluationArtifact, dict[str, Any]]:
    from chia.base.ChiaFunction import get

    root.mkdir(parents=True, exist_ok=True)
    identity = build_finalization_identity(candidate, target, config, baseline)
    cached, attempt, rejections = _cached_finalization(root, identity)
    if rejections:
        dump_json(root / "CACHE_REJECTIONS.json", rejections)
    if cached is not None:
        attempts = _finalization_attempts(root, identity)
        matching_epochs = [
            item.get("completed_epoch")
            for item in attempts
            if item.get("completed_epoch") is not None
            and item.get("status") == cached.status
            and bool(item.get("final_valid")) == bool(cached.final_valid)
            and item.get("failure_class") == cached.failure_class
        ]
        return cached, {
            "reused": True,
            "attempt": None,
            "identity": identity,
            "cache_rejections": rejections,
            "completed_epoch": min(matching_epochs) if matching_epochs else None,
            "attempts": attempts,
        }
    attempt_dir = root / f"attempt-{attempt:02d}"
    dump_json(attempt_dir / "IDENTITY.json", identity)
    final = get(node.run.chia_remote(
        node,
        candidate=candidate,
        early=early,
        target=target,
        config=config,
        baseline=baseline,
        output_dir=str(attempt_dir),
        _chia_display_name=display_name + f":attempt-{attempt:02d}",
    ))
    final.hashes["finalization_identity"] = identity["fingerprint"]
    final.hashes["candidate_source_sha256"] = identity["candidate_source_sha256"]
    if final.candidate_id != candidate.id:
        raise RuntimeError("finalization returned a different candidate identity")
    dump_json(attempt_dir / "evaluation.json", final)
    completed_epoch = time.time()
    dump_json(attempt_dir / "METADATA.json", {
        "completed_epoch": completed_epoch,
        "active_seconds": final.active_seconds,
    })
    metadata = {
        "reused": False,
        "attempt": attempt,
        "identity": identity,
        "cache_rejections": rejections,
        "completed_epoch": completed_epoch,
        "active_seconds": final.active_seconds,
        "attempts": _finalization_attempts(root, identity),
    }
    dump_json(root / "LATEST.json", metadata)
    return final, metadata


class BoomFinalizationNode:
    @ChiaFunction(
        resources={"chipyard": 1, "boom_sim": 1, "boom_vivado": 1, "verilator_run": 1},
        max_retries=0,
    )
    def run(
        self,
        *,
        candidate: CandidateArtifact,
        early: EvaluationArtifact,
        target: dict[str, Any],
        config: dict[str, Any],
        baseline: dict[str, Any],
        output_dir: str,
    ) -> EvaluationArtifact:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        final = EvaluationArtifact.from_dict(early.to_dict())
        # Preserve the search PPA only as context.  Finalization is a separate
        # measurement and must not inherit search stages or active time.
        final.status = "candidate_invalid"
        final.stage = "materialize"
        final.failure_class = None
        final.retryable = False
        final.raw_error = ""
        final.build_ok = False
        final.interface_ok = False
        final.correctness_ok = False
        final.candidate_valid = False
        final.promotable = False
        final.final_valid = False
        final.valid_improvement = False
        final.post_route = None
        final.differential = None
        final.regression = None
        final.stages = []
        final.hashes = {}
        final.active_seconds = 0.0
        try:
            with acquire_workspace(config["remote"]) as lease:
                workspace = lease.workspace
                elaboration = BoomElaborationNode().run(
                    candidate=candidate, target=target, config=config,
                    workspace=str(workspace), output_dir=str(output / "elaboration"),
                )
                final.append_stage(elaboration)
                final.hashes.update(elaboration.payload.get("rtl_hashes", {}))
                final.candidate_source_path = elaboration.payload.get(
                    "candidate_source_path"
                )
                final.candidate_diff_path = elaboration.payload.get(
                    "candidate_diff_path"
                )
                if not elaboration.success:
                    dump_json(output / "evaluation.json", final)
                    return final
                final.build_ok = True
                differential_results = []
                for seed in (42, 43, 44):
                    differential = BoomDifferentialNode().run(
                        target=target, config=config,
                        rtl_dir=elaboration.payload["rtl_dir"], seed=seed,
                        output_dir=str(output / f"differential-seed-{seed}"),
                        cycles=1_000_000,
                        workspace_slot=str(lease.slot_root),
                    )
                    final.append_stage(differential)
                    differential_results.append(differential.payload.get("differential"))
                    if not differential.success:
                        final.differential = {"seeds": differential_results}
                        dump_json(output / "evaluation.json", final)
                        return final
                final.correctness_ok = True
                final.interface_ok = all(
                    isinstance(item, dict) and item.get("interface_ok")
                    for item in differential_results
                )
                final.differential = {"seeds": differential_results, "passed": True}
                route = BoomVivadoNode().run(
                    target=target, config=config,
                    rtl_dir=elaboration.payload["rtl_dir"],
                    output_dir=str(output / "vivado-route"), route=True,
                    workspace_slot=str(lease.slot_root),
                )
                final.append_stage(route)
                final.post_route = route.payload.get("post_route")
                if not route.success:
                    dump_json(output / "evaluation.json", final)
                    return final

                regression = BoomRegressionNode().run(
                    candidate=candidate, config=config, workspace=str(workspace),
                    output_dir=str(output / "regression"),
                )
                final.append_stage(regression)
                final.regression = regression.payload.get("regression")
                if not regression.success:
                    # append_stage already preserves the regression node's
                    # authoritative status, failure class, and retryability.
                    dump_json(output / "evaluation.json", final)
                    return final
                passed = True
                final.stage = "regression"
                final.status = "complete"
                final.failure_class = None
                final.retryable = False
                final.raw_error = ""
                area_ok = (
                    final.post_route["slice_luts"]
                    <= baseline.get("post_route_slice_luts", baseline["slice_luts"])
                    * baseline.get("maximum_lut_ratio", 1.05)
                )
                final.final_valid = bool(passed and area_ok)
                update_validity(final, baseline)
                dump_json(output / "evaluation.json", final)
                return final
        except Exception as exc:
            final.status = "infra_blocked"
            final.stage = "regression"
            final.failure_class = "finalization_infrastructure_failure"
            final.retryable = True
            final.raw_error = f"{type(exc).__name__}: {exc}"
            dump_json(output / "evaluation.json", final)
            return final


class BoomRegressionNode:
    """Full MegaBOOM regression through CHIA's official Chipyard nodes."""

    @ChiaFunction(resources={"chipyard": 1, "verilator_run": 1}, max_retries=0)
    def run(
        self,
        *,
        candidate: CandidateArtifact,
        config: dict[str, Any],
        workspace: str,
        output_dir: str,
    ) -> StageResult:
        import time

        started = time.monotonic()
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        chipyard = Path(workspace)
        try:
            _activate_chipyard_environment(chipyard, config)
            builder = ChiselBuildNode(
                chipyard_path=str(chipyard), config=config["base"]["config"],
                config_package="chipyard", target=BuildTarget.VERILATOR,
                make_jobs=int(config["physical"]["cpus_per_job"]),
                timeout_seconds=int(config["physical"]["regression_timeout_seconds"]),
                clean=True, collect_generated_src=False, name=candidate.id,
            )
            build = builder.build()
            build_stdout = output / "build.stdout"
            build_stderr = output / "build.stderr"
            build_stdout.write_text(build.stdout)
            build_stderr.write_text(build.stderr)
            if not build.success:
                return StageResult(
                    stage="regression", success=False, status="candidate_invalid",
                    failure_class="full_chipyard_build_failure", retryable=False,
                    message=(build.stderr + "\n" + build.stdout)[-32_000:],
                    stdout_path=str(build_stdout), stderr_path=str(build_stderr),
                    active_seconds=time.monotonic() - started,
                )
            simulator_path = output / build.simulator_binary_name
            simulator_path.write_bytes(build.simulator_binary_content)
            simulator_path.chmod(0o755)
            simulator_sha = hashlib.sha256(build.simulator_binary_content).hexdigest()
            rsort = regression_binary(config)
            run = VerilatorRunNode().run(
                artifact=build, test_binary_content=rsort.read_bytes(),
                test_binary_name=rsort.name, work_dir=str(output / "verilator"),
                timeout_seconds=int(config["physical"]["regression_timeout_seconds"]),
                verbose=False, cleanup_task_dir=True,
            )
            (output / "rsort.log").write_text(run.log)
            (output / "rsort.out").write_text(run.out)
            passed = _verilator_run_passed(run)
            return StageResult(
                stage="regression", success=passed,
                status="complete" if passed else "candidate_invalid",
                failure_class=None if passed else "full_processor_regression_failure",
                retryable=False,
                message="" if passed else (run.log + "\n" + run.out)[-32_000:],
                stdout_path=str(output / "rsort.log"), stderr_path=str(output / "rsort.out"),
                payload={
                    "regression": {
                        "success": run.success, "returncode": run.returncode,
                        "passed_marker": "*** PASSED ***" in run.log,
                        "normal_finish_marker": "$finish" in (run.log + run.out),
                        "acceptance_rule": "chia_verilator_run_success_and_returncode_zero",
                        "simulator_sha256": simulator_sha,
                        "simulator_path": str(simulator_path),
                    }
                },
                active_seconds=time.monotonic() - started,
            )
        except Exception as exc:
            return StageResult(
                stage="regression", success=False, status="infra_blocked",
                failure_class="regression_infrastructure_failure", retryable=True,
                message=f"{type(exc).__name__}: {exc}",
                active_seconds=time.monotonic() - started,
            )


def reconcile_interactive_finalization(
    config: dict[str, Any], output: Path,
) -> dict[str, Any]:
    """Reclassify a completed run after fixing the regression acceptance rule.

    This operation never reruns or edits the candidate.  It preserves the
    original JSON, verifies the raw CHIA run outcome, and changes only the
    pass/fail interpretation that had required a marker absent from rsort.
    """
    interactive_manifest = output / "INTERACTIVE_MANIFEST.json"
    if interactive_manifest.exists():
        config = load_frozen_run_config(
            config,
            root=output / "frozen",
            run_manifest=interactive_manifest,
            schema_version="interactive-frozen-run-v1",
        )
    final_path = output / "FINAL_RESULT.json"
    evaluation_path = output / "finalization/evaluation.json"
    if not final_path.exists() or not evaluation_path.exists():
        raise FileNotFoundError("completed interactive finalization evidence is missing")

    original_final_sha = sha256_file(final_path)
    original_evaluation_sha = sha256_file(evaluation_path)
    preserved_final = output / "FINAL_RESULT.pre-v13.9.json"
    preserved_evaluation = output / "finalization/evaluation.pre-v13.9.json"
    if not preserved_final.exists():
        preserved_final.write_bytes(final_path.read_bytes())
    if not preserved_evaluation.exists():
        preserved_evaluation.write_bytes(evaluation_path.read_bytes())

    final = EvaluationArtifact.from_dict(load_json(evaluation_path))
    regression = final.regression or {}
    if not (regression.get("success") and regression.get("returncode") == 0):
        raise RuntimeError("raw CHIA VerilatorRunNode result is not successful")
    if not (final.build_ok and final.correctness_ok and final.post_route):
        raise RuntimeError("non-regression finalization gates are incomplete")

    regression["normal_finish_marker"] = "$finish" in (
        (output / "finalization/regression/rsort.log").read_text(errors="replace")
        + (output / "finalization/regression/rsort.out").read_text(errors="replace")
    )
    regression["acceptance_rule"] = "chia_verilator_run_success_and_returncode_zero"
    final.regression = regression
    for stage in final.stages:
        if stage.get("stage") == "regression":
            stage.update({
                "success": True,
                "status": "complete",
                "failure_class": None,
                "retryable": False,
                "message": "",
            })
            stage.setdefault("payload", {})["regression"] = regression

    if len(config["targets"]) != 1:
        raise ValueError("interactive reconciliation requires exactly one target")
    target_id = next(iter(config["targets"]))
    from .frozen import baseline_input

    baseline = load_json(baseline_input(config, target_id, "baseline-ppa.json"))
    baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
    area_ok = (
        final.post_route["slice_luts"]
        <= baseline.get("post_route_slice_luts", baseline["slice_luts"])
        * baseline["maximum_lut_ratio"]
    )
    final.status = "complete"
    final.stage = "regression"
    final.failure_class = None
    final.retryable = False
    final.raw_error = ""
    final.final_valid = bool(area_ok)
    update_validity(final, baseline)
    dump_json(evaluation_path, final)

    previous = load_json(preserved_final)
    payload = {
        "candidate_id": previous["candidate_id"],
        "status": "complete" if final.final_valid else final.status,
        "final_valid": final.final_valid,
        "valid_improvement": final.valid_improvement,
        "evaluation": final.to_dict(),
    }
    dump_json(final_path, payload)
    reconciliation = {
        "version": "v13.9-regression-gate-fix",
        "candidate_changed": False,
        "eda_rerun": False,
        "reason": (
            "CHIA VerilatorRunNode defines success as returncode zero; "
            "rsort.riscv normally reaches $finish without a PASSED banner."
        ),
        "original_final_sha256": original_final_sha,
        "original_evaluation_sha256": original_evaluation_sha,
        "preserved_final_sha256": sha256_file(preserved_final),
        "preserved_evaluation_sha256": sha256_file(preserved_evaluation),
        "corrected_final_sha256": sha256_file(final_path),
        "corrected_evaluation_sha256": sha256_file(evaluation_path),
        "raw_regression": regression,
    }
    dump_json(output / "RECONCILIATION.json", reconciliation)
    return payload


def finalize_campaign(campaign: Path) -> list[dict[str, Any]]:
    manifest = json.loads((campaign / "manifest.json").read_text())
    verify_frozen_run(
        campaign / "frozen", manifest.get("frozen_run_fingerprint")
    )
    config = manifest["config"]
    node = BoomFinalizationNode()
    results = []
    for item in manifest["schedule"]:
        run_path = campaign / "runs" / item["id"]
        state_path = run_path / "run.json"
        state = json.loads(state_path.read_text())
        target = config["targets"][state["target"]]
        baseline = json.loads(
            (campaign / "frozen/targets" / state["target"] / "baseline-ppa.json").read_text()
        )
        baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
        options = []
        for row in state["candidates"]:
            evaluation_data = search_evaluation_data(row)
            if not row.get("candidate") or not evaluation_data:
                continue
            evaluation = EvaluationArtifact.from_dict(evaluation_data)
            if evaluation.candidate_valid:
                options.append((row, evaluation))
        if not options:
            state["finalization"] = {"status": "no_valid_candidate"}
            dump_json(state_path, state)
            results.append(state["finalization"])
            continue
        row, early = min(
            options,
            key=lambda item: (
                item[1].post_synth["critical_delay_ns"],
                item[1].post_synth["slice_luts"],
            ),
        )
        candidate = CandidateArtifact.from_dict(row["candidate"])
        final_dir = run_path / "finalization"
        final, finalization_meta = finalize_candidate(
            node=node,
            candidate=candidate,
            early=early,
            target=target,
            config=config,
            baseline=baseline,
            root=final_dir,
            display_name=f"finalize:{candidate.id}",
        )
        state["finalization"] = {
            "status": "complete" if final.final_valid else final.status,
            "candidate_id": candidate.id,
            "evaluation": final.to_dict(),
            "metadata": finalization_meta,
        }
        state["status"] = "complete" if final.final_valid else "finalization_failed"
        state["last_finalization_query_epoch"] = time.time()
        completed_epoch = finalization_meta.get("completed_epoch")
        if completed_epoch is not None:
            state["finalization_completed_epoch"] = completed_epoch
        if final.final_valid and completed_epoch is not None:
            state.setdefault("first_final_acceptance_epoch", completed_epoch)
        dump_json(state_path, state)
        results.append(state["finalization"])
    return results
