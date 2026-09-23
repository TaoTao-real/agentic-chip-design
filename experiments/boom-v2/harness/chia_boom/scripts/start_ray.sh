#!/usr/bin/env bash
set -euo pipefail

: "${DEEPSEEK_API_KEY:?export DEEPSEEK_API_KEY before starting Ray}"

RAY_CPUS="${RAY_CPUS:-24}"
ray stop --force >/dev/null 2>&1 || true
ray start \
  --head \
  --port="${RAY_PORT:-6379}" \
  --dashboard-host=127.0.0.1 \
  --num-cpus="${RAY_CPUS}" \
  --resources='{"chipyard":2,"boom_sim":2,"boom_vivado":2,"verilator_run":2,"deepseek_creds":1}'
ray status
