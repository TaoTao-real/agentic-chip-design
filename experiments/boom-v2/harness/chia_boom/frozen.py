from __future__ import annotations

import copy
import importlib.metadata
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .artifacts import dump_json, sha256_file
from .core import canonical_hash
from .environment import vivado_environment_command


TOOL_FILES = (
    "differential_test.py",
    "vivado_ooc_route.tcl",
    "vivado_ooc_synth_only.tcl",
)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _run_root(config: dict[str, Any]) -> Path | None:
    value = config.get("frozen_run_root")
    return Path(value) if value else None


def baseline_input(config: dict[str, Any], target_id: str, name: str) -> Path:
    root = _run_root(config)
    if root:
        return root / "targets" / target_id / name
    return Path(config["remote"]["frozen_inputs_root"]) / target_id / name


def baseline_rtl(config: dict[str, Any], target_id: str) -> Path:
    root = _run_root(config)
    if root:
        return root / "targets" / target_id / "baseline-rtl"
    return Path(config["remote"]["baseline_rtl_root"]) / target_id


def tool_file(config: dict[str, Any], name: str) -> Path:
    root = _run_root(config)
    if root:
        return root / "tools" / name
    return Path(config["remote"]["tools_root"]) / name


def regression_binary(config: dict[str, Any]) -> Path:
    root = _run_root(config)
    if root:
        return root / "tests" / "rsort.riscv"
    return Path(config["remote"]["rsort_binary"])


def freeze_run_inputs(
    config: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Copy every executable reference into a private run snapshot.

    The returned config resolves golden RTL, tools, tests, patches, and replay
    inputs only through ``root``.  Callers may add treatment-specific files
    before sealing the directory with :func:`seal_frozen_run`.
    """
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"frozen run root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    frozen_config = copy.deepcopy(config)
    for target_id in sorted(config["targets"]):
        for name in (
            "baseline-source.scala",
            "baseline-timing.txt",
            "baseline-ppa.json",
        ):
            _copy_file(
                baseline_input(config, target_id, name),
                root / "targets" / target_id / name,
            )
        source_rtl = baseline_rtl(config, target_id)
        if not source_rtl.is_dir():
            raise RuntimeError(f"qualified baseline RTL is missing: {source_rtl}")
        shutil.copytree(
            source_rtl, root / "targets" / target_id / "baseline-rtl"
        )
    for name in TOOL_FILES:
        _copy_file(tool_file(config, name), root / "tools" / name)
    _copy_file(regression_binary(config), root / "tests" / "rsort.riscv")

    required_patch = config.get("remote", {}).get("required_patch")
    if required_patch:
        frozen_patch = root / "base" / "required.patch"
        _copy_file(Path(required_patch), frozen_patch)
        frozen_config["remote"]["required_patch"] = str(frozen_patch.resolve())

    replay_root_value = config.get("remote", {}).get("replay_root")
    if replay_root_value:
        replay_source = Path(replay_root_value)
        frozen_replay = root / "q1-replay"
        if replay_source.is_dir():
            shutil.copytree(replay_source, frozen_replay)
        else:
            frozen_replay.mkdir(parents=True)
        frozen_config["remote"]["replay_root"] = str(frozen_replay.resolve())

    frozen_config["frozen_run_root"] = str(root.resolve())
    return frozen_config


def immutable_contract(config: dict[str, Any]) -> dict[str, Any]:
    physical_keys = (
        "tool", "part", "clock_period_ns", "maximum_lut_ratio",
        "candidate_timeout_seconds", "route_timeout_seconds",
        "regression_timeout_seconds",
        "cpus_per_job",
    )
    return {
        "base": config["base"],
        "physical": {
            key: config["physical"].get(key) for key in physical_keys
        },
        "targets": config["targets"],
        "qualification": config.get("qualification", {}),
        "resource_policy": {
            "parallel_runs": config.get("search", {}).get("parallel_runs"),
            "infrastructure_attempt_limit": config.get("search", {}).get(
                "infrastructure_attempt_limit"
            ),
        },
    }


def runtime_tool_versions(config: dict[str, Any]) -> dict[str, str]:
    """Capture stable version strings for tools covered by qualification."""
    commands = {
        "python": "python3 --version",
        "java": "java -version",
        "verilator": "verilator --version",
        "vivado": vivado_environment_command(config) + "; vivado -version | head -3",
    }
    versions = {}
    for name, command in commands.items():
        result = subprocess.run(
            ["bash", "-lc", command], text=True, capture_output=True, timeout=120
        )
        if result.returncode:
            raise RuntimeError(
                f"cannot identify {name}: {(result.stderr or result.stdout)[-2000:]}"
            )
        versions[name] = (result.stdout + result.stderr).strip()
    for distribution in ("chialoops", "ray"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"cannot identify {distribution}") from exc
    return versions


def qualification_request(config: dict[str, Any]) -> dict[str, Any]:
    files: dict[str, str] = {}
    for target_id in sorted(config["targets"]):
        path = baseline_input(config, target_id, "baseline-source.scala")
        files[f"targets/{target_id}/baseline-source.scala"] = sha256_file(path)
    for name in TOOL_FILES:
        files[f"tools/{name}"] = sha256_file(tool_file(config, name))
    files["tests/rsort.riscv"] = sha256_file(regression_binary(config))
    required_patch = config.get("remote", {}).get("required_patch")
    if required_patch:
        files["base/required.patch"] = sha256_file(required_patch)
    replay_root_value = config.get("remote", {}).get("replay_root")
    if replay_root_value:
        replay_root = Path(replay_root_value)
        if replay_root.is_dir():
            for path in sorted(replay_root.rglob("*")):
                if path.is_file():
                    files[f"q1-replay/{path.relative_to(replay_root)}"] = sha256_file(path)
    value = {"contract": immutable_contract(config), "files": files}
    return value | {"fingerprint": canonical_hash(value)}


def qualified_target_artifact_hashes(
    config: dict[str, Any], target_id: str
) -> dict[str, str]:
    files: dict[str, str] = {}
    for name in ("baseline-source.scala", "baseline-timing.txt", "baseline-ppa.json"):
        path = baseline_input(config, target_id, name)
        files[f"targets/{target_id}/{name}"] = sha256_file(path)
    rtl_root = baseline_rtl(config, target_id)
    for path in sorted(rtl_root.rglob("*")):
        if path.is_file():
            files[
                f"targets/{target_id}/baseline-rtl/{path.relative_to(rtl_root)}"
            ] = sha256_file(path)
    return files


def qualified_artifact_hashes(config: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    for target_id in sorted(config["targets"]):
        files.update(qualified_target_artifact_hashes(config, target_id))
    for name in TOOL_FILES:
        files[f"tools/{name}"] = sha256_file(tool_file(config, name))
    files["tests/rsort.riscv"] = sha256_file(regression_binary(config))
    replay_root_value = config.get("remote", {}).get("replay_root")
    if replay_root_value:
        replay_root = Path(replay_root_value)
        if replay_root.is_dir():
            for path in sorted(replay_root.rglob("*")):
                if path.is_file():
                    files[f"q1-replay/{path.relative_to(replay_root)}"] = sha256_file(path)
    return files


def verify_qualification(
    config: dict[str, Any],
    evidence: dict[str, Any],
    *,
    tool_versions: dict[str, str] | None = None,
) -> None:
    if not evidence.get("passed"):
        raise RuntimeError("Q0/Q1 qualification did not pass")
    request = qualification_request(config)
    if evidence.get("qualification_fingerprint") != request["fingerprint"]:
        raise RuntimeError("qualification contract fingerprint does not match config")
    actual = qualified_artifact_hashes(config)
    if evidence.get("qualified_artifact_hashes") != actual:
        raise RuntimeError("qualified source, golden, test, or tool artifacts changed")
    current_versions = tool_versions or runtime_tool_versions(config)
    if evidence.get("tool_versions") != current_versions:
        raise RuntimeError("qualified compiler, simulator, or EDA versions changed")


def directory_hashes(root: Path) -> dict[str, str]:
    ignored = {"SHA256SUMS.json", "FROZEN_RUN_MANIFEST.json"}
    return {
        str(path.relative_to(root)): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in ignored
    }


def seal_frozen_run(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    files = directory_hashes(root)
    value = {
        "schema_version": "frozen-run-v1",
        "contract": immutable_contract(config),
        "files": files,
    }
    value["fingerprint"] = canonical_hash(value)
    dump_json(root / "FROZEN_RUN_MANIFEST.json", value)
    dump_json(root / "SHA256SUMS.json", files)
    return value


def verify_frozen_run(root: Path, expected_fingerprint: str | None = None) -> dict[str, Any]:
    manifest_path = root / "FROZEN_RUN_MANIFEST.json"
    if not manifest_path.exists():
        raise RuntimeError("frozen run manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    actual_files = directory_hashes(root)
    if manifest.get("files") != actual_files:
        raise RuntimeError("frozen run artifact drift detected")
    without_fingerprint = {key: value for key, value in manifest.items() if key != "fingerprint"}
    fingerprint = canonical_hash(without_fingerprint)
    if manifest.get("fingerprint") != fingerprint:
        raise RuntimeError("frozen run manifest fingerprint is invalid")
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise RuntimeError("campaign frozen run fingerprint changed")
    return manifest


def load_frozen_run_config(
    requested_config: dict[str, Any],
    *,
    root: Path,
    run_manifest: Path,
    schema_version: str,
) -> dict[str, Any]:
    """Load a saved run config after binding it to the requested contract."""
    manifest = json.loads(run_manifest.read_text())
    if manifest.get("schema_version") != schema_version:
        raise RuntimeError("frozen run metadata schema changed")
    if manifest.get("source_contract") != immutable_contract(requested_config):
        raise RuntimeError("interactive immutable contract changed")
    config = manifest["config"]
    if Path(config.get("frozen_run_root", "")).resolve() != root.resolve():
        raise RuntimeError("interactive frozen root identity changed")
    verify_frozen_run(root, manifest.get("frozen_run_fingerprint"))
    return config
