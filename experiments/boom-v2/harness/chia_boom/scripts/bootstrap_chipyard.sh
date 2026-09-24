#!/usr/bin/env bash
set -euo pipefail

CHIPYARD_COMMIT="4ab72313087580a44d647b52923389a06ec0712f"
BOOM_COMMIT="3229345a4f6388562f81ce1955deefe1a4f9acbd"
CHIPYARD_REPOSITORY="${CHIPYARD_REPOSITORY:-https://github.com/ucb-bar/chipyard.git}"
SLOT_COUNT=2
RUN_SETUP=false

usage() {
  cat <<'EOF'
Usage: bootstrap_chipyard.sh [--slots N] [--run-setup]

Required environment:
  CHIPYARD_WORKSPACE_ROOT  Parent directory for slot-01/chipyard, ...

Options:
  --slots N     Create N independent checkouts (default: 2)
  --run-setup   Run the pinned checkout's lean build-setup in every slot

This script never reads or writes DEEPSEEK_API_KEY.
EOF
}

while (($#)); do
  case "$1" in
    --slots)
      SLOT_COUNT="$2"
      shift 2
      ;;
    --run-setup)
      RUN_SETUP=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

: "${CHIPYARD_WORKSPACE_ROOT:?set CHIPYARD_WORKSPACE_ROOT}"
[[ "${SLOT_COUNT}" =~ ^[1-9][0-9]*$ ]] || {
  echo "--slots must be a positive integer" >&2
  exit 2
}
command -v git >/dev/null

mkdir -p "${CHIPYARD_WORKSPACE_ROOT}"
for slot_number in $(seq 1 "${SLOT_COUNT}"); do
  slot=$(printf 'slot-%02d' "${slot_number}")
  checkout="${CHIPYARD_WORKSPACE_ROOT}/${slot}/chipyard"
  if [[ -e "${checkout}" ]]; then
    echo "refusing to replace existing checkout: ${checkout}" >&2
    exit 2
  fi
  mkdir -p "$(dirname "${checkout}")"
  git clone "${CHIPYARD_REPOSITORY}" "${checkout}"
  git -C "${checkout}" checkout --detach "${CHIPYARD_COMMIT}"
  git -C "${checkout}" submodule sync --recursive
  git -C "${checkout}" submodule update --init --recursive
  actual_chipyard=$(git -C "${checkout}" rev-parse HEAD)
  actual_boom=$(git -C "${checkout}/generators/boom" rev-parse HEAD)
  [[ "${actual_chipyard}" == "${CHIPYARD_COMMIT}" ]] || {
    echo "Chipyard revision mismatch in ${checkout}" >&2
    exit 3
  }
  [[ "${actual_boom}" == "${BOOM_COMMIT}" ]] || {
    echo "BOOM revision mismatch in ${checkout}" >&2
    exit 3
  }
  if [[ "${RUN_SETUP}" == true ]]; then
    (
      cd "${checkout}"
      ./build-setup.sh --use-lean-conda --skip-submodules riscv-tools
    )
    [[ -f "${checkout}/env.sh" ]] || {
      echo "Chipyard setup did not create env.sh in ${checkout}" >&2
      exit 3
    }
  fi
  printf '%s chipyard=%s boom=%s setup=%s\n' \
    "${slot}" "${actual_chipyard}" "${actual_boom}" "${RUN_SETUP}"
done
