#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from chia_boom.environment import load_config


def git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    workspaces = sorted(Path(config["remote"]["workspace_slots"]).glob("slot-*/chipyard"))
    if not workspaces:
        raise SystemExit("no workspace matched workspace_slots/slot-*/chipyard")

    expected_chipyard = config["base"]["chipyard_commit"]
    expected_boom = config["base"]["boom_commit"]
    for workspace in workspaces:
        if git_head(workspace) != expected_chipyard:
            raise SystemExit(f"Chipyard revision mismatch in {workspace}")
        boom = workspace / "generators/boom"
        if git_head(boom) != expected_boom:
            raise SystemExit(f"BOOM revision mismatch in {boom}")

    source_workspace = workspaces[0]
    frozen = Path(config["remote"]["frozen_inputs_root"])
    manifest: dict[str, object] = {
        "chipyard_commit": expected_chipyard,
        "boom_commit": expected_boom,
        "targets": {},
    }
    for target_id, target in sorted(config["targets"].items()):
        source = source_workspace / target["mutable_file"]
        if not source.is_file():
            raise SystemExit(f"target source is missing: {source}")
        output = frozen / target_id / "baseline-source.scala"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(source.read_bytes())
        manifest["targets"][target_id] = {
            "mutable_file": target["mutable_file"],
            "baseline_source_sha256": sha256(output),
        }

    Path(config["remote"]["baseline_rtl_root"]).mkdir(parents=True, exist_ok=True)
    Path(config["remote"]["campaigns_root"]).mkdir(parents=True, exist_ok=True)
    manifest_path = frozen / "INPUT_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(manifest_path)


if __name__ == "__main__":
    main()
