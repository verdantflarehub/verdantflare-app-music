#!/usr/bin/env bash
set -euo pipefail

readonly model_repository=MiniMaxAI/MiniMax-H3
readonly model_revision=42ed227ee7df40d41602854ae760620d6eb651fe
readonly lora_repository=lightx2v/Minimax-h3-Turbo
readonly lora_revision=05ef678438e84933c406131b59abbf86919b3aac
readonly lora_filename=minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors
readonly expected_lora_sha256=9e642fc8749c74f8da5e2382877ab5c7aa37b9a73b7fd0d6d457bd1b3cb1ae99
readonly model_path="${H3_MODEL_PATH:-/models/MiniMax-H3-Diffusers}"
readonly lora_dir="${H3_LORA_DIR:-/models/MiniMax-H3-Turbo}"

mkdir -p "${model_path}" "${lora_dir}"

MODEL_REPOSITORY="${model_repository}" \
MODEL_REVISION="${model_revision}" \
MODEL_PATH="${model_path}" \
LORA_REPOSITORY="${lora_repository}" \
LORA_REVISION="${lora_revision}" \
LORA_FILENAME="${lora_filename}" \
LORA_DIR="${lora_dir}" \
python3 <<'PY'
import os

from huggingface_hub import hf_hub_download, snapshot_download

snapshot_download(
    repo_id=os.environ["MODEL_REPOSITORY"],
    revision=os.environ["MODEL_REVISION"],
    local_dir=os.environ["MODEL_PATH"],
    allow_patterns=[
        "LICENSE",
        "model_index.json",
        "modular_model_index.json",
        "processor/*",
        "tokenizer/*",
        "text_encoder/*",
        "vae/*",
        "audio_vae/*",
        "transformer_ref/*",
        "scheduler/*",
        "audio_scheduler/*",
    ],
)
hf_hub_download(
    repo_id=os.environ["LORA_REPOSITORY"],
    filename=os.environ["LORA_FILENAME"],
    revision=os.environ["LORA_REVISION"],
    local_dir=os.environ["LORA_DIR"],
)
PY

readonly lora_path="${lora_dir}/${lora_filename}"
actual_lora_sha256="$(sha256sum "${lora_path}" | awk '{print $1}')"
if [[ "${actual_lora_sha256}" != "${expected_lora_sha256}" ]]; then
    printf 'Unexpected LightX2V LoRA SHA-256: %s\n' "${actual_lora_sha256}" >&2
    exit 1
fi

printf '%s\n' "${model_revision}" >"${model_path}/.verdantflare-model-revision"
printf '%s\n' "${lora_revision}" >"${lora_dir}/.verdantflare-model-revision"
printf 'MiniMax H3 Diffusers model: %s\n' "${model_path}"
printf 'LightX2V Ref2VA LoRA: %s\n' "${lora_path}"
