#!/usr/bin/env bash
set -euo pipefail

readonly MODEL_REPOSITORY="MiniMaxAI/MiniMax-H3"
readonly MODEL_REVISION="42ed227ee7df40d41602854ae760620d6eb651fe"
readonly REVISION_FILE="${MODEL_PATH}/.verdantflare-model-revision"
export MODEL_PATH MODEL_REPOSITORY MODEL_REVISION

mapfile -t gpu_uuids < <(nvidia-smi --query-gpu=uuid --format=csv,noheader | sed '/^[[:space:]]*$/d')
if (( ${#gpu_uuids[@]} != H3_VISIBLE_GPU_COUNT )); then
    echo "MiniMax H3 requires exactly ${H3_VISIBLE_GPU_COUNT} visible CUDA GPUs; found ${#gpu_uuids[@]}." >&2
    exit 1
fi

unique_gpu_count="$(printf '%s\n' "${gpu_uuids[@]}" | sort -u | wc -l)"
if (( unique_gpu_count != H3_VISIBLE_GPU_COUNT )); then
    echo "MiniMax H3 requires ${H3_VISIBLE_GPU_COUNT} distinct physical CUDA GPUs." >&2
    exit 1
fi

installed_revision=""
if [[ -f "${REVISION_FILE}" ]]; then
    installed_revision="$(<"${REVISION_FILE}")"
fi

if [[ "${installed_revision}" != "${MODEL_REVISION}" || ! -f "${MODEL_PATH}/Ref2VA/model_index.json" ]]; then
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
        "model_index.json",
        "Ref2VA/*",
    ],
)
PY
    printf '%s\n' "${MODEL_REVISION}" >"${REVISION_FILE}"
fi

exec /opt/nvidia/nvidia_entrypoint.sh sglang serve \
    --model-path "${MODEL_PATH}" \
    --model-variant ref2va \
    --num-gpus "${H3_VISIBLE_GPU_COUNT}" \
    --tp-size "${H3_VISIBLE_GPU_COUNT}" \
    --ulysses-degree 1 \
    --encoder-parallel fold \
    --performance-mode memory \
    --layerwise-offload-components dit,text_encoder,vae \
    --dit-offload-prefetch-size 1 \
    --dit-layerwise-resident-layers 12 \
    --enable-torch-compile false \
    --host 0.0.0.0 \
    --port 8000 \
    "$@"
