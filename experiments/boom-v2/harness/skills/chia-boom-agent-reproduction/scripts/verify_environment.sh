#!/usr/bin/env bash
set -euo pipefail

: "${AGENTIC_CHIP_LAB_ROOT:?set AGENTIC_CHIP_LAB_ROOT}"
: "${VIVADO_SETTINGS:?set VIVADO_SETTINGS}"

INSTALL_ROOT="${AGENTIC_CHIP_LAB_ROOT}/boom-public-v1"
PYTHON_BIN="${PYTHON_BIN:-python}"
QUALIFICATION="${QUALIFICATION:-${INSTALL_ROOT}/qualification/QUALIFICATION.json}"

for path in "${VIVADO_SETTINGS}" "${QUALIFICATION}"; do
  [[ -e "${path}" ]] || { echo "MISSING ${path}" >&2; exit 2; }
done

"${PYTHON_BIN}" --version
ray --version
verilator --version | head -n 1
java -version 2>&1 | head -n 1
source "${VIVADO_SETTINGS}"
vivado -version | head -n 2

"${PYTHON_BIN}" - "${QUALIFICATION}" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
if not data.get("passed"):
    raise SystemExit("qualification evidence is not passing")
if int(data.get("qualified_parallel_runs", 0)) < 1:
    raise SystemExit("no qualified physical run slot")
print(f"qualified_parallel_runs={data['qualified_parallel_runs']}")
print(f"combined_peak_tree_rss_bytes={data.get('combined_peak_tree_rss_bytes')}")
PY

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "credential=missing"
  exit 2
fi
echo "credential=present"
