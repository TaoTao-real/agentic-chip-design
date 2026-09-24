from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

import psutil

from chia.base.ChiaFunction import ChiaFunction, get

from .artifacts import CandidateArtifact, dump_json, load_json, sha256_file
from .environment import vivado_environment_command
from .frozen import (
    qualification_request,
    qualified_artifact_hashes,
    qualified_target_artifact_hashes,
    runtime_tool_versions,
)
from .nodes import (
    BoomCandidateEvaluationNode,
    BoomElaborationNode,
    BoomVivadoNode,
    acquire_workspace,
)


class _MemorySampler:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.peak = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        root = psutil.Process()
        while not self.stop.wait(0.5):
            processes = [root]
            try:
                processes.extend(root.children(recursive=True))
            except psutil.Error:
                pass
            rss = 0
            for process in processes:
                try:
                    rss += process.memory_info().rss
                except psutil.Error:
                    pass
            self.peak = max(self.peak, rss)

    def __enter__(self) -> "_MemorySampler":
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


class BoomQualificationTargetNode:
    @ChiaFunction(
        resources={"chipyard": 1, "boom_vivado": 1},
        max_retries=0,
    )
    def run(
        self,
        *,
        target: dict[str, Any],
        config: dict[str, Any],
        qualification_input_fingerprint: str,
        tool_versions: dict[str, str],
        output_dir: str,
    ) -> dict[str, Any]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        source_path = Path(config["remote"]["frozen_inputs_root"]) / target["id"] / "baseline-source.scala"
        candidate = CandidateArtifact(
            campaign_id="qualification", target=target["id"], arm="Q0", seed=0,
            index=0, parent_id=None, source=source_path.read_text(),
            diff="baseline-qualification", diagnosis=[{
                "evidence": "baseline", "source_region": "none",
                "hypothesis": "none", "predicted_effect": "none", "risk": "none",
            }], selected_hypothesis=0, visible_feedback="",
            request_sha256="", response_sha256="", usage=None,
        )
        repeats = []
        peak = 0
        with _MemorySampler() as memory:
            with acquire_workspace(config["remote"]) as lease:
                workspace = lease.workspace
                for repeat in range(1, 4):
                    repeat_dir = output / f"repeat-{repeat}"
                    elaboration = BoomElaborationNode().run(
                        candidate=candidate, target=target, config=config,
                        workspace=str(workspace), output_dir=str(repeat_dir / "elaboration"),
                    )
                    if not elaboration.success:
                        result = {
                            "target": target["id"], "passed": False,
                            "failure": elaboration.to_dict(), "repeats": repeats,
                            "qualification_input_fingerprint": qualification_input_fingerprint,
                            "tool_versions": tool_versions,
                        }
                        dump_json(output / "qualification.json", result)
                        return result
                    route = BoomVivadoNode().run(
                        target=target, config=config,
                        rtl_dir=elaboration.payload["rtl_dir"],
                        output_dir=str(repeat_dir / "vivado"), route=True,
                        workspace_slot=str(lease.slot_root),
                    )
                    if not route.success:
                        result = {
                            "target": target["id"], "passed": False,
                            "failure": route.to_dict(), "repeats": repeats,
                            "qualification_input_fingerprint": qualification_input_fingerprint,
                            "tool_versions": tool_versions,
                        }
                        dump_json(output / "qualification.json", result)
                        return result
                    post_synth = json.loads(
                        json.dumps(route.payload["post_route"])
                    )
                    # The route script also emits post-synthesis reports.
                    from .nodes import parse_vivado_ppa

                    post_synth = parse_vivado_ppa(
                        repeat_dir / "vivado/post_synth_timing_summary.rpt",
                        repeat_dir / "vivado/post_synth_utilization.rpt",
                        float(config["physical"]["clock_period_ns"]),
                    )
                    repeats.append(
                        {"repeat": repeat, "post_synth": post_synth, "post_route": route.payload["post_route"]}
                    )
                    if repeat == 1:
                        baseline_rtl = Path(config["remote"]["baseline_rtl_root"]) / target["id"]
                        if baseline_rtl.exists():
                            shutil.rmtree(baseline_rtl)
                        shutil.copytree(elaboration.payload["rtl_dir"], baseline_rtl)
                        timing_source = repeat_dir / "vivado/post_synth_timing_paths.rpt"
                        timing_target = Path(config["remote"]["frozen_inputs_root"]) / target["id"] / "baseline-timing.txt"
                        shutil.copy2(timing_source, timing_target)
            peak = memory.peak
        synth_delays = [item["post_synth"]["critical_delay_ns"] for item in repeats]
        route_delays = [item["post_route"]["critical_delay_ns"] for item in repeats]
        def spread(values: list[float]) -> float:
            return 100 * (max(values) - min(values)) / min(values)
        passed = spread(synth_delays) <= 1.0 and spread(route_delays) <= 1.0
        baseline_ppa = {
            **repeats[0]["post_synth"],
            "stage": "post_synth",
            "post_route_critical_delay_ns": repeats[0]["post_route"]["critical_delay_ns"],
            "post_route_slice_luts": repeats[0]["post_route"]["slice_luts"],
            "repeat_synth_spread_percent": spread(synth_delays),
            "repeat_route_spread_percent": spread(route_delays),
            "repeat_count": 3,
        }
        baseline_path = Path(config["remote"]["frozen_inputs_root"]) / target["id"] / "baseline-ppa.json"
        dump_json(baseline_path, baseline_ppa)
        result = {
            "target": target["id"], "passed": passed, "repeats": repeats,
            "peak_tree_rss_bytes": peak,
            "baseline_ppa_sha256": sha256_file(baseline_path),
            "qualified_target_artifact_hashes": qualified_target_artifact_hashes(
                config, target["id"]
            ),
            "qualification_input_fingerprint": qualification_input_fingerprint,
            "tool_versions": tool_versions,
        }
        dump_json(output / "qualification.json", result)
        return result


class BoomQualificationResourceNode:
    """Measure one representative elaboration+synthesis job in isolation."""

    @ChiaFunction(
        resources={"chipyard": 1, "boom_vivado": 1},
        max_retries=0,
    )
    def run(
        self,
        *,
        target: dict[str, Any],
        config: dict[str, Any],
        output_dir: str,
        probe_id: int,
    ) -> dict[str, Any]:
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        source_path = (
            Path(config["remote"]["frozen_inputs_root"])
            / target["id"]
            / "baseline-source.scala"
        )
        candidate = CandidateArtifact(
            campaign_id="qualification-resource-probe",
            target=target["id"], arm="Q0-resource", seed=probe_id,
            index=probe_id, parent_id=None, source=source_path.read_text(),
            diff="baseline-resource-probe",
            diagnosis=[{
                "evidence": "baseline", "source_region": "none",
                "hypothesis": "none", "predicted_effect": "none", "risk": "none",
            }],
            selected_hypothesis=0, visible_feedback="",
            request_sha256="", response_sha256="", usage=None,
        )
        failure: dict[str, Any] | None = None
        with _MemorySampler() as memory:
            with acquire_workspace(config["remote"]) as lease:
                elaboration = BoomElaborationNode().run(
                    candidate=candidate,
                    target=target,
                    config=config,
                    workspace=str(lease.workspace),
                    output_dir=str(output / "elaboration"),
                )
                if not elaboration.success:
                    failure = elaboration.to_dict()
                else:
                    synthesis = BoomVivadoNode().run(
                        target=target,
                        config=config,
                        rtl_dir=elaboration.payload["rtl_dir"],
                        output_dir=str(output / "vivado"),
                        route=False,
                        workspace_slot=str(lease.slot_root),
                    )
                    if not synthesis.success:
                        failure = synthesis.to_dict()
        result = {
            "probe_id": probe_id,
            "passed": failure is None,
            "peak_tree_rss_bytes": memory.peak,
        }
        if failure is not None:
            result["failure"] = failure
        return result


def qualified_parallel_runs(
    requested: int,
    probes: list[dict[str, Any]],
    *,
    memory_limit_bytes: int = 24 * 1024 ** 3,
) -> tuple[int, int]:
    """Open two slots only after two concurrently launched probes fit."""
    if requested < 2:
        peak = max((int(item.get("peak_tree_rss_bytes", 0)) for item in probes), default=0)
        return 1, peak
    combined = sum(int(item.get("peak_tree_rss_bytes", 0)) for item in probes)
    passed = len(probes) == 2 and all(item.get("passed") for item in probes)
    return (2 if passed and combined <= memory_limit_bytes else 1), combined


def qualify(config: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    tool_versions = runtime_tool_versions(config)
    input_fingerprint = qualification_request(config)["fingerprint"]
    dump_json(output / "TOOL_VERSIONS.json", tool_versions)
    versions = subprocess.run(
        ["bash", "-lc", (
            "hostname; nproc; free -b; python3 --version; java -version; "
            "verilator --version; " + vivado_environment_command(config) + "; "
            "vivado -version | head -3"
        )],
        text=True, capture_output=True, timeout=300,
    )
    (output / "environment.stdout").write_text(versions.stdout)
    (output / "environment.stderr").write_text(versions.stderr)
    node = BoomQualificationTargetNode()
    targets_by_id: dict[str, dict[str, Any]] = {}
    pending: list[tuple[str, Any]] = []
    for target_id, target in sorted(config["targets"].items()):
        prior = _load_reusable_target_qualification(
            config,
            output,
            target_id,
            input_fingerprint=input_fingerprint,
            tool_versions=tool_versions,
        )
        if prior is not None:
            targets_by_id[target_id] = prior
            continue
        ref = node.run.chia_remote(
            node,
            target=target,
            config=config,
            qualification_input_fingerprint=input_fingerprint,
            tool_versions=tool_versions,
            output_dir=str(output / target_id),
            _chia_display_name=f"qualify:{target_id}",
        )
        pending.append((target_id, ref))
    # Upstream Chia currently unwraps one profiled result at a time; passing a
    # list to get() leaves each element wrapped in _ProfiledResult.
    for target_id, ref in pending:
        targets_by_id[target_id] = get(ref)
    targets = [targets_by_id[target_id] for target_id in sorted(config["targets"])]
    requested_parallel_runs = min(
        2, max(1, int(config.get("search", {}).get("parallel_runs", 1)))
    )
    resource_probes: list[dict[str, Any]] = []
    if requested_parallel_runs >= 2 and all(item["passed"] for item in targets):
        probe_node = BoomQualificationResourceNode()
        target = config["targets"][sorted(config["targets"])[0]]
        probe_refs = [
            probe_node.run.chia_remote(
                probe_node,
                target=target,
                config=config,
                output_dir=str(output / f"resource-probe-{probe_id}"),
                probe_id=probe_id,
                _chia_display_name=f"qualify:resource-probe-{probe_id}",
            )
            for probe_id in (1, 2)
        ]
        # Both tasks are submitted before either result is collected, so the
        # measurement exercises the two physical resource slots concurrently.
        resource_probes = [get(ref) for ref in probe_refs]
    slots, combined_peak = qualified_parallel_runs(
        requested_parallel_runs, resource_probes
    )
    replay_root = Path(config["remote"]["replay_root"])
    replay_required = bool(
        config.get("qualification", {}).get("q1_replay_required", False)
    )
    if all(item["passed"] for item in targets) and (replay_root / "manifest.json").exists():
        replay = replay_q1_fixtures(config, output / "q1-replay")
    else:
        replay = {
            "passed": not replay_required,
            "skipped": True,
            "reason": "private replay fixtures were not mounted",
        }
    result = {
        "passed": versions.returncode == 0 and all(item["passed"] for item in targets) and replay["passed"],
        "environment_returncode": versions.returncode,
        "targets": targets,
        "combined_peak_tree_rss_bytes": combined_peak,
        "qualified_parallel_runs": slots,
        "resource_probes": resource_probes,
        "q1_replay": replay,
    }
    request = qualification_request(config)
    result["qualification_fingerprint"] = request["fingerprint"]
    result["qualification_request"] = request
    result["qualified_artifact_hashes"] = qualified_artifact_hashes(config)
    result["tool_versions"] = tool_versions
    dump_json(output / "QUALIFICATION.json", result)
    return result


def _load_reusable_target_qualification(
    config: dict[str, Any],
    output: Path,
    target_id: str,
    *,
    input_fingerprint: str,
    tool_versions: dict[str, str],
) -> dict[str, Any] | None:
    """Reuse only a complete, hash-verified Q0 result after driver failure."""
    result_path = output / target_id / "qualification.json"
    baseline_path = (
        Path(config["remote"]["frozen_inputs_root"])
        / target_id / "baseline-ppa.json"
    )
    timing_path = (
        Path(config["remote"]["frozen_inputs_root"])
        / target_id / "baseline-timing.txt"
    )
    if not result_path.exists() or not baseline_path.exists() or not timing_path.exists():
        return None
    try:
        result = load_json(result_path)
        if (
            not result.get("passed")
            or result.get("target") != target_id
            or len(result.get("repeats", [])) != 3
            or result.get("qualification_input_fingerprint") != input_fingerprint
            or result.get("tool_versions") != tool_versions
            or result.get("baseline_ppa_sha256") != sha256_file(baseline_path)
            or result.get("qualified_target_artifact_hashes")
            != qualified_target_artifact_hashes(config, target_id)
            or timing_path.stat().st_size == 0
        ):
            return None
        for repeat in result["repeats"]:
            repeat_number = int(repeat["repeat"])
            vivado = output / target_id / f"repeat-{repeat_number}" / "vivado"
            for stage in ("post_synth", "post_route"):
                metrics = repeat[stage]
                timing = vivado / f"{stage}_timing_summary.rpt"
                utilization = vivado / f"{stage}_utilization.rpt"
                if (
                    not timing.exists()
                    or not utilization.exists()
                    or metrics.get("timing_report_sha256") != sha256_file(timing)
                    or metrics.get("utilization_report_sha256") != sha256_file(utilization)
                ):
                    return None
        result["reused_after_driver_failure"] = True
        return result
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return None


def replay_q1_fixtures(config: dict[str, Any], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    replay_root = Path(config["remote"]["replay_root"])
    manifest = json.loads((replay_root / "manifest.json").read_text())
    node = BoomCandidateEvaluationNode()
    rows = []
    for index, fixture in enumerate(manifest["fixtures"], 1):
        target = config["targets"][fixture["target"]]
        source = (replay_root / fixture["source"]).read_text()
        baseline_source = (
            Path(config["remote"]["frozen_inputs_root"])
            / fixture["target"] / "baseline-source.scala"
        ).read_text()
        import difflib

        diff = "".join(
            difflib.unified_diff(
                baseline_source.splitlines(True), source.splitlines(True),
                fromfile="a/" + target["mutable_file"],
                tofile="b/" + target["mutable_file"],
            )
        )
        candidate = CandidateArtifact(
            campaign_id="q1-replay", target=fixture["target"], arm="Q1",
            seed=41, index=index, parent_id=None, source=source, diff=diff,
            diagnosis=[{
                "evidence": "saved v12 fixture", "source_region": "saved fixture",
                "hypothesis": "replay only", "predicted_effect": "replay only",
                "risk": "replay only",
            }], selected_hypothesis=0, visible_feedback="",
            request_sha256="", response_sha256=sha256_file(replay_root / fixture["source"]),
            usage=None,
        )
        baseline = json.loads(
            (
                Path(config["remote"]["frozen_inputs_root"])
                / fixture["target"] / "baseline-ppa.json"
            ).read_text()
        )
        baseline["maximum_lut_ratio"] = config["physical"]["maximum_lut_ratio"]
        evaluation = get(
            node.evaluate.chia_remote(
                node, candidate=candidate, target=target, config=config,
                baseline=baseline, output_dir=str(output / fixture["id"]),
                _chia_display_name=f"q1-replay:{fixture['id']}",
            )
        )
        row = {"fixture": fixture, "evaluation": evaluation.to_dict()}
        rows.append(row)
        dump_json(output / fixture["id"] / "replay-result.json", row)
    index = {row["fixture"]["id"]: row["evaluation"] for row in rows}
    checks: dict[str, bool] = {}
    for fixture in manifest["fixtures"]:
        actual = index[fixture["id"]]
        expected = fixture.get("expected_failure_class")
        if expected:
            checks[f"{fixture['id']}_specific"] = actual["failure_class"] == expected
        if fixture.get("require_raw_error"):
            checks[f"{fixture['id']}_raw_error_preserved"] = bool(
                actual.get("raw_error")
            )
    result = {"passed": all(checks.values()), "checks": checks, "rows": rows}
    dump_json(output / "REPLAY.json", result)
    return result
