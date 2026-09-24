from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests
import psutil

from .artifacts import load_json
from .core import validate_config
from .frozen import TOOL_FILES, verify_qualification


def _command(command: list[str], *, timeout: int = 120) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    text = (result.stdout + result.stderr).strip()
    return result.returncode == 0, text[-4000:]


def _git_head(path: Path) -> tuple[bool, str]:
    return _command(["git", "-C", str(path), "rev-parse", "HEAD"])


def _official_model_check(config: dict[str, Any]) -> tuple[bool, str]:
    """Check model availability without persisting or returning the key."""
    key_env = config["model"]["api_key_env"]
    key = os.environ.get(key_env, "").strip()
    if not key:
        return False, f"missing environment variable {key_env}"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            config["model"]["api_base"].rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + key},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        payload = response.json()
        models = {
            item.get("id") for item in payload.get("data", [])
            if isinstance(item, dict)
        }
        model = config["model"]["id"]
        return model in models, (
            f"model {model} is available"
            if model in models else f"model {model} is absent"
        )
    except (requests.RequestException, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def doctor(
    config: dict[str, Any],
    *,
    require_qualification: bool = False,
    check_api: bool = False,
) -> dict[str, Any]:
    """Inspect a deployment without ever serializing the API-key value."""
    validate_config(config)
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, passed: bool, detail: str, *, required: bool = True) -> None:
        checks[name] = {
            "passed": bool(passed),
            "required": required,
            "detail": detail,
        }

    supported_python = (3, 10) <= sys.version_info[:2] < (3, 14)
    record(
        "python",
        supported_python,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    required_cpus = int(config["physical"]["cpus_per_job"]) * min(
        2, max(1, int(config.get("search", {}).get("parallel_runs", 1)))
    )
    logical_cpus = psutil.cpu_count(logical=True) or 0
    record(
        "logical_cpus",
        logical_cpus >= required_cpus,
        f"found={logical_cpus} required={required_cpus}",
    )
    total_memory = int(psutil.virtual_memory().total)
    record(
        "memory",
        total_memory >= 32 * 1024 ** 3,
        f"total_bytes={total_memory} required_bytes={32 * 1024 ** 3}",
    )
    disk_path = Path(config["remote"]["install_root"])
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    free_disk = int(shutil.disk_usage(disk_path).free)
    record(
        "disk",
        free_disk >= 200 * 1024 ** 3,
        f"path={disk_path}; free_bytes={free_disk}; required_bytes={200 * 1024 ** 3}",
    )
    for executable in ("git", "ray", "verilator", "java"):
        found = shutil.which(executable)
        record(f"executable:{executable}", bool(found), found or "not found")

    conda_setup = Path(config["environment"]["conda_setup"])
    vivado_settings = Path(config["environment"]["vivado_settings"])
    record("conda_setup", conda_setup.is_file(), str(conda_setup))
    record("vivado_settings", vivado_settings.is_file(), str(vivado_settings))
    if vivado_settings.is_file():
        ok, detail = _command([
            "bash", "-lc",
            f"source {shlex.quote(str(vivado_settings))}; vivado -version | head -3",
        ])
        record("vivado", ok, detail)

    slots_root = Path(config["remote"]["workspace_slots"])
    workspaces = sorted(slots_root.glob("slot-*/chipyard"))
    required_slots = min(
        2, max(1, int(config.get("search", {}).get("parallel_runs", 1)))
    )
    record(
        "workspace_count",
        len(workspaces) >= required_slots,
        f"found={len(workspaces)} required={required_slots}",
    )
    for index, workspace in enumerate(workspaces, 1):
        ok, head = _git_head(workspace)
        record(
            f"workspace:{index}:chipyard_commit",
            ok and head.strip() == config["base"]["chipyard_commit"],
            head.strip() if ok else head,
        )
        boom = workspace / "generators/boom"
        ok, head = _git_head(boom)
        record(
            f"workspace:{index}:boom_commit",
            ok and head.strip() == config["base"]["boom_commit"],
            head.strip() if ok else head,
        )
        record(
            f"workspace:{index}:env",
            (workspace / "env.sh").is_file(),
            str(workspace / "env.sh"),
        )
        for target_id, target in sorted(config["targets"].items()):
            source = workspace / target["mutable_file"]
            record(
                f"workspace:{index}:target:{target_id}",
                source.is_file(), str(source),
            )

    for name in TOOL_FILES:
        path = Path(config["remote"]["tools_root"]) / name
        record(f"harness_tool:{name}", path.is_file(), str(path))
    regression = Path(config["remote"]["rsort_binary"])
    record("regression_binary", regression.is_file(), str(regression))

    key_env = config["model"]["api_key_env"]
    key_present = bool(os.environ.get(key_env, "").strip())
    record(
        "model_credential",
        key_present if check_api else True,
        f"environment_variable={key_env}; present={str(key_present).lower()}",
        required=check_api,
    )
    if check_api:
        ok, detail = _official_model_check(config)
        record("model_api", ok, detail)

    qualification_path = (
        Path(config["remote"]["install_root"])
        / "qualification/QUALIFICATION.json"
    )
    if require_qualification:
        if qualification_path.is_file():
            try:
                verify_qualification(config, load_json(qualification_path))
                record("qualification", True, "passing and current")
            except Exception as exc:
                record("qualification", False, f"{type(exc).__name__}: {exc}")
        else:
            record("qualification", False, f"missing {qualification_path}")
    else:
        record(
            "qualification",
            True,
            "not required for this phase",
            required=False,
        )

    passed = all(
        item["passed"] for item in checks.values() if item["required"]
    )
    return {
        "schema_version": "deployment-doctor-v1",
        "passed": passed,
        "api_key_value_recorded": False,
        "checks": checks,
    }
