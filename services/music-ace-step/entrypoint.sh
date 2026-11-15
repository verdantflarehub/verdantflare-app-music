#!/usr/bin/env bash
set -euo pipefail

readonly EXTERNAL_HOST="${ACESTEP_API_HOST:-0.0.0.0}"
readonly EXTERNAL_PORT="${ACESTEP_API_PORT:-8001}"
readonly INTERNAL_PORT="18001"
readonly GUARD_STATE_DIR="${VF_GPU_GUARD_STATE_DIR:-/tmp/acestep-guard}"

mkdir -p "${GUARD_STATE_DIR}"
export ACESTEP_UPSTREAM_URL="http://127.0.0.1:${INTERNAL_PORT}"
export PYTHONPATH="/opt/acestep-guard:${PYTHONPATH:-}"
export VF_CONTAINER_INIT_PID="$$"
export VF_UPSTREAM_PID_FILE="${GUARD_STATE_DIR}/upstream.pid"
export VF_GPU_GUARD_STATE_PATH="${VF_GPU_GUARD_STATE_PATH:-${GUARD_STATE_DIR}/gpu_guard_state.json}"

ACESTEP_API_HOST="127.0.0.1" ACESTEP_API_PORT="${INTERNAL_PORT}" \
    /app/.venv/bin/python -m acestep.api_server "$@" &
acestep_pid=$!
printf '%s\n' "${acestep_pid}" >"${VF_UPSTREAM_PID_FILE}"
export VF_UPSTREAM_LEADER_PID="${acestep_pid}"

/app/.venv/bin/python -m uvicorn guard.proxy:app --host "${EXTERNAL_HOST}" --port "${EXTERNAL_PORT}" &
guard_pid=$!

term_handler() {
    kill -TERM "${guard_pid}" "${acestep_pid}" 2>/dev/null || true
    wait "${guard_pid}" "${acestep_pid}" 2>/dev/null || true
}
trap term_handler SIGTERM SIGINT

wait -n "${acestep_pid}" "${guard_pid}"
exit_code=$?
term_handler
exit "${exit_code}"
