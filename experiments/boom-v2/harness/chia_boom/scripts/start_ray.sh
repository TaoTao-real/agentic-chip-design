#!/usr/bin/env bash
set -euo pipefail

: "${DEEPSEEK_API_KEY:?export DEEPSEEK_API_KEY before starting Ray}"

RAY_CPUS="${RAY_CPUS:-24}"
if ray status >/dev/null 2>&1; then
  if [[ "${RESET_RAY:-0}" != "1" ]]; then
    echo "an existing Ray cluster is running; set RESET_RAY=1 only on the dedicated experiment host" >&2
    exit 2
  fi
  ray stop --force >/dev/null
fi
ray start \
  --head \
  --port="${RAY_PORT:-6379}" \
  --dashboard-host=127.0.0.1 \
  --num-cpus="${RAY_CPUS}" \
  --resources='{"chipyard":2,"boom_sim":2,"boom_vivado":2,"verilator_run":2,"deepseek_creds":1}'
ray status
