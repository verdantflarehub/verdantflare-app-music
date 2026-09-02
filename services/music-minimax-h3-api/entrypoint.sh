#!/usr/bin/env bash
set -euo pipefail

readonly model_repository="MiniMaxAI/MiniMax-H3"
readonly model_revision="42ed227ee7df40d41602854ae760620d6eb651fe"
readonly model_path="${MODEL_PATH:-/models/MiniMax-H3}"
readonly revision_file="${model_path}/.verdantflare-revision"
readonly transformer_weights_path="${model_path}/serialized-int8/diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors"
readonly output_path="${H3_OUTPUT_PATH:-/data/projects/h3/tasks}"

mapfile -t gpu_uuids < <(nvidia-smi --query-gpu=uuid --format=csv,noheader | sed '/^[[:space:]]*$/d')
if (( ${#gpu_uuids[@]} != H3_VISIBLE_GPU_COUNT )); then
    echo "MiniMax H3 Full INT8 requires exactly ${H3_VISIBLE_GPU_COUNT} visible CUDA GPU; found ${#gpu_uuids[@]}." >&2
    exit 1
fi

if [[ ! -f "${revision_file}" ]] || [[ "$(<"${revision_file}")" != "${model_revision}" ]]; then
    mkdir -p "${model_path}"
    MODEL_REPOSITORY="${model_repository}" \
    MODEL_REVISION="${model_revision}" \
    MODEL_PATH="${model_path}" \
    python3 - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id=os.environ["MODEL_REPOSITORY"],
    revision=os.environ["MODEL_REVISION"],
    local_dir=os.environ["MODEL_PATH"],
    allow_patterns=["LICENSE", "README.md", "model_index.json", "Ref2VA/*"],
)
PY
    printf '%s\n' "${model_revision}" >"${revision_file}"
fi

if [[ ! -f "${transformer_weights_path}" ]]; then
    echo "MiniMax H3 Full INT8 weights do not exist: ${transformer_weights_path}" >&2
    exit 1
fi

mkdir -p "${output_path}"

exec /opt/nvidia/nvidia_entrypoint.sh sglang serve \
    --model-type diffusion \
    --model-path "${model_path}" \
    --model-variant ref2va \
    --transformer-weights-path "${transformer_weights_path}" \
    --num-gpus 1 \
    --tp-size 1 \
    --ulysses-degree 1 \
    --encoder-parallel auto \
    --attention-backend fa \
    --performance-mode memory \
    --layerwise-offload-components dit,text_encoder \
    --dit-offload-prefetch-size 1 \
    --dit-layerwise-resident-layers 0 \
    --enable-torch-compile false \
    --warmup-mode off \
    --output-path "${output_path}" \
    --host 0.0.0.0 \
    --port 8000 \
    "$@"
