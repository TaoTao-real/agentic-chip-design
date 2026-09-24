#!/usr/bin/env bash
set -euo pipefail

: "${CONFIG:?set CONFIG to the expanded experiment JSON}"

PHASE="${1:-prequal}"
case "${PHASE}" in
  prequal)
    exec chia-boom doctor --config "${CONFIG}"
    ;;
  postqual)
    exec chia-boom doctor --config "${CONFIG}" --require-qualification
    ;;
  api)
    exec chia-boom doctor --config "${CONFIG}" --check-api
    ;;
  *)
    echo "usage: CONFIG=... $0 [prequal|postqual|api]" >&2
    exit 2
    ;;
esac
