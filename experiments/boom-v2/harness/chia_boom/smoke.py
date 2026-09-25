from __future__ import annotations

import json
import shutil
import shlex
import sys
from pathlib import Path
from typing import Any

from .artifacts import dump_json, load_json, sha256_file
from .frozen import (
    baseline_rtl,
    freeze_run_inputs,
    seal_frozen_run,
    tool_file,
    verify_frozen_run,
    verify_qualification,
)
from .nodes import _run


def run_baseline_smoke(
    config: dict[str, Any],
    *,
    config_path: Path,
    output: Path,
    target_id: str | None = None,
    cycles: int = 10_000,
    seed: int = 20_260_924,
    jobs: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the frozen golden RTL against itself without a model call."""
    config_path = config_path.resolve(strict=True)
    output = output.resolve()
    if cycles < 128:
        raise ValueError("smoke cycles must be at least 128")
    if output.exists() and any(output.iterdir()):
        if not force:
            raise FileExistsError(f"smoke output is not empty: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    targets = sorted(config["targets"])
    target_id = target_id or (targets[0] if len(targets) == 1 else None)
    if not target_id or target_id not in config["targets"]:
        raise ValueError("select one configured target with --target")
    target = config["targets"][target_id]

    qualification_path = (
        Path(config["remote"]["install_root"])
        / "qualification/QUALIFICATION.json"
    )
    qualification = load_json(qualification_path)
    verify_qualification(config, qualification)

    frozen_root = output / "frozen"
    frozen_config = freeze_run_inputs(config, frozen_root)
    shutil.copy2(qualification_path, frozen_root / "QUALIFICATION.json")
    frozen_manifest = seal_frozen_run(frozen_root, frozen_config)
    verify_frozen_run(frozen_root, frozen_manifest["fingerprint"])

    run_dir = output / "differential"
    stdout_path = output / "smoke.stdout"
    stderr_path = output / "smoke.stderr"
    golden = baseline_rtl(frozen_config, target_id)
    command = [
        sys.executable,
        str(tool_file(frozen_config, "differential_test.py")),
        "--baseline", str(golden),
        "--candidate", str(golden),
        "--top", target["rtl_top"],
        "--out", str(run_dir),
        "--cycles", str(cycles),
        "--seed", str(seed),
        "--scenario", target_id,
        "--jobs", str(jobs or config["physical"]["cpus_per_job"]),
    ]
    returncode, stdout, stderr, elapsed = _run(
        shlex.join(command),
        cwd=output,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout=int(config["physical"]["candidate_timeout_seconds"]),
    )
    result_path = run_dir / "result.json"
    differential = (
        json.loads(result_path.read_text()) if result_path.is_file() else None
    )
    passed = bool(
        returncode == 0
        and differential
        and differential.get("passed")
        and differential.get("interface_ok")
    )
    verify_frozen_run(frozen_root, frozen_manifest["fingerprint"])
    result = {
        "schema_version": "baseline-smoke-v1",
        "passed": passed,
        "target": target_id,
        "cycles": cycles,
        "seed": seed,
        "returncode": returncode,
        "elapsed_seconds": elapsed,
        "model_calls": 0,
        "api_key_used": False,
        "frozen_run_fingerprint": frozen_manifest["fingerprint"],
        "hashes": {
            "config": sha256_file(config_path),
            "qualification": sha256_file(qualification_path),
            "frozen_manifest": sha256_file(
                frozen_root / "FROZEN_RUN_MANIFEST.json"
            ),
            "differential_result": (
                sha256_file(result_path) if result_path.is_file() else None
            ),
        },
        "differential": differential,
        "error_tail": (stderr or stdout)[-4000:],
    }
    dump_json(output / "SMOKE.json", result)
    return result
