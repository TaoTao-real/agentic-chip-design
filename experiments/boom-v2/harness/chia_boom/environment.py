from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any


def _expand(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    """Load a trusted experiment config and expand environment-backed paths."""
    return _expand(json.loads(Path(path).read_text()))


def chipyard_environment_command(config: dict[str, Any], chipyard: Path) -> str:
    setup = config.get("environment", {})
    commands = ["set +u"]
    if setup.get("conda_setup"):
        commands.append(f"source {shlex.quote(str(setup['conda_setup']))}")
    commands.append(f"source {shlex.quote(str(chipyard / 'env.sh'))}")
    return "; ".join(commands)


def vivado_environment_command(config: dict[str, Any]) -> str:
    settings = config.get("environment", {}).get("vivado_settings")
    return f"source {shlex.quote(str(settings))}" if settings else ":"
