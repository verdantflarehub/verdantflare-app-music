#!/usr/bin/env bash
set -euo pipefail

readonly MODEL_REPOSITORY="MiniMaxAI/MiniMax-Music3"
readonly MODEL_REVISION="fbdf52fbaaca799592917417eb05f1899f1255ec"
readonly REVISION_FILE="${MODEL_PATH}/.verdantflare-model-revision"
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

exec /opt/nvidia/nvidia_entrypoint.sh sgl-omni serve \
    --model-path "${MODEL_PATH}" \
    --model-name "${MODEL_REPOSITORY}" \
    --host 0.0.0.0 \
    --port 8000 \
    "$@"
