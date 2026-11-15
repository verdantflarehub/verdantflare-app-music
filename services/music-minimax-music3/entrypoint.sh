#!/usr/bin/env bash
set -euo pipefail

readonly MODEL_REPOSITORY="MiniMaxAI/MiniMax-Music3"
readonly MODEL_REVISION="fbdf52fbaaca799592917417eb05f1899f1255ec"
readonly REVISION_FILE="${MODEL_PATH}/.verdantflare-model-revision"
readonly GUARD_STATE_DIR="${VF_GPU_GUARD_STATE_DIR:-/tmp/music3-guard}"
export MODEL_REPOSITORY MODEL_REVISION MODEL_PATH

gpu_count="$(python3 -c 'import torch; print(torch.cuda.device_count())')"
if (( gpu_count < 2 )); then
    echo "MiniMax Music 3 requires at least two visible CUDA GPUs; found ${gpu_count}." >&2
    exit 1
fi

installed_revision=""
if [[ -f "${REVISION_FILE}" ]]; then
    installed_revision="$(<"${REVISION_FILE}")"
fi

if [[ "${installed_revision}" != "${MODEL_REVISION}" || ! -f "${MODEL_PATH}/config.json" ]]; then
    mkdir -p "${MODEL_PATH}"
    python3 <<'PY'
import os

from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ["MODEL_REPOSITORY"],
    revision=os.environ["MODEL_REVISION"],
    local_dir=os.environ["MODEL_PATH"],
    allow_patterns=[
        "LICENSE",
        "README.md",
        "config.json",
        "dav.pth",
        "flowmatching_vae.pth",
        "qwen_7B/*",
    ],
)
PY
    printf '%s\n' "${MODEL_REVISION}" >"${REVISION_FILE}"
fi

mkdir -p "${GUARD_STATE_DIR}"
export MUSIC3_UPSTREAM_URL="http://127.0.0.1:18000"
export PYTHONPATH="/opt/music3-guard:${PYTHONPATH:-}"
export VF_CONTAINER_INIT_PID="$$"
export VF_UPSTREAM_PID_FILE="${GUARD_STATE_DIR}/upstream.pid"
export VF_GPU_GUARD_STATE_PATH="${VF_GPU_GUARD_STATE_PATH:-${GUARD_STATE_DIR}/gpu_guard_state.json}"

/opt/nvidia/nvidia_entrypoint.sh sgl-omni serve \
    --model-path "${MODEL_PATH}" \
    --model-name "${MODEL_REPOSITORY}" \
    --host 127.0.0.1 \
    --port 18000 \
    "$@" &
sgl_pid=$!
printf '%s\n' "${sgl_pid}" >"${VF_UPSTREAM_PID_FILE}"
export VF_UPSTREAM_LEADER_PID="${sgl_pid}"

python3 -m uvicorn guard.proxy:app --host 0.0.0.0 --port 8000 &
guard_pid=$!

term_handler() {
    kill -TERM "${guard_pid}" "${sgl_pid}" 2>/dev/null || true
    wait "${guard_pid}" "${sgl_pid}" 2>/dev/null || true
}
trap term_handler SIGTERM SIGINT

wait -n "${sgl_pid}" "${guard_pid}"
exit_code=$?
term_handler
exit "${exit_code}"
