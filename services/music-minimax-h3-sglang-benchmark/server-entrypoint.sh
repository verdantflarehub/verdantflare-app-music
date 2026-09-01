#!/usr/bin/env bash
set -euo pipefail

readonly model_repository="MiniMaxAI/MiniMax-H3"
readonly model_revision="42ed227ee7df40d41602854ae760620d6eb651fe"
readonly model_path="${MODEL_PATH:-/models/MiniMax-H3}"
readonly revision_file="${model_path}/.verdantflare-revision"
readonly server_port="${H3_SERVER_PORT:-8000}"
readonly output_path="${H3_SERVER_OUTPUT_PATH:?H3_SERVER_OUTPUT_PATH is required}"
readonly quantization="${H3_QUANTIZATION:-bf16}"

if [[ "${H3_VISIBLE_GPU_COUNT:-1}" != "1" ]]; then
    echo "The official RTX 4090 benchmark requires exactly one visible GPU." >&2
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

quantization_args=()
case "${quantization}" in
    bf16)
        ;;
    kitchen_int8)
        quantization_args=(--quantization kitchen_int8)
        ;;
    *)
        echo "H3_QUANTIZATION must be bf16 or kitchen_int8, got: ${quantization}" >&2
        exit 1
        ;;
esac

lora_args=()
if [[ -n "${H3_LORA_PATH:-}" ]]; then
    : "${H3_LORA_WEIGHT_NAME:?H3_LORA_WEIGHT_NAME is required with H3_LORA_PATH}"
    lora_args=(
        --lora-path "${H3_LORA_PATH}"
        --lora-weight-name "${H3_LORA_WEIGHT_NAME}"
        --lora-nickname "${H3_LORA_NICKNAME:-h3-benchmark}"
        --lora-scale "${H3_LORA_SCALE:-1.0}"
        --lora-merge-mode auto
    )
    if [[ -n "${H3_LORA_ALPHA:-}" ]]; then
        lora_args+=(--lora-alpha "${H3_LORA_ALPHA}")
    fi
fi

mkdir -p "${output_path}"

exec /opt/nvidia/nvidia_entrypoint.sh sglang serve \
    --model-path "${model_path}" \
    --model-variant ref2va \
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
    --port "${server_port}" \
    "${quantization_args[@]}" \
    "${lora_args[@]}" \
    "$@"
