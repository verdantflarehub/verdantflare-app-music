#!/usr/bin/env bash
set -euo pipefail

readonly MODEL_REVISION="e6d0c1a17da07c33557852f9dfa2bd44cc75737d"
readonly MODEL_BASE_URL="https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/${MODEL_REVISION}"

download_runtime_file() {
    local source_path="$1"
    local target_path="$2"
    local target_directory
    target_directory="${RVC_RUNTIME_ROOT}/$(dirname "${target_path}")"
    local filename
    filename="$(basename "${target_path}")"
    local target="${RVC_RUNTIME_ROOT}/${target_path}"
    local temporary="${target}.part"

    if [[ -s "${target}" ]]; then
        return
    fi

    mkdir -p "${target_directory}"
    echo "Downloading ${source_path} to ${target}"
    aria2c --console-log-level=error --continue=true -x 16 -s 16 -k 1M \
        "${MODEL_BASE_URL}/${source_path}" \
        -d "${target_directory}" \
        -o "${filename}.part"
    test -s "${temporary}"
    mv -f "${temporary}" "${target}"
}

python3 - <<'PY'
import sys

import torch

if not torch.cuda.is_available():
    print("music-rvc-api requires a visible CUDA GPU", file=sys.stderr)
    raise SystemExit(1)
PY

mkdir -p "${RVC_VOICE_ROOT}" "${RVC_RUNTIME_ROOT}" "${RVC_TEMP_ROOT}" \
    assets/hubert assets/rmvpe
download_runtime_file hubert_base.pt hubert_base.pt
download_runtime_file rmvpe.pt rmvpe.pt
download_runtime_file pretrained_v2/f0G40k.pth pretrained_v2/f0G40k.pth
download_runtime_file pretrained_v2/f0D40k.pth pretrained_v2/f0D40k.pth

ln -sfn "${RVC_RUNTIME_ROOT}/hubert_base.pt" assets/hubert/hubert_base.pt
ln -sfn "${RVC_RUNTIME_ROOT}/rmvpe.pt" assets/rmvpe/rmvpe.pt

export TEMP="${RVC_TEMP_ROOT}"
export index_root="${RVC_VOICE_ROOT}"
export rmvpe_root="${RVC_RUNTIME_ROOT}"
export weight_root="${RVC_VOICE_ROOT}"

exec python3 -m uvicorn verdantflare_rvc.api:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    "$@"
