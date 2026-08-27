#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
import sys
import torch

if not torch.cuda.is_available():
    print("music-uvr5-api requires a visible CUDA GPU", file=sys.stderr)
    raise SystemExit(1)
PY

mkdir -p "${UVR5_MODEL_ROOT}" "${UVR5_TEMP_ROOT}"

readonly MODEL_RELEASE_URL="https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs"

install_model_file() {
    local filename="$1"
    local expected_size="$2"
    local target="${UVR5_MODEL_ROOT}/${filename}"
    local partial="${target}.part"

    if [[ -f "${target}" ]]; then
        if [[ "$(stat --format='%s' "${target}")" != "${expected_size}" ]]; then
            echo "UVR5 model file has an unexpected size: ${target}" >&2
            return 1
        fi
        return
    fi

    curl --fail --location --show-error \
        --retry 5 --retry-all-errors --continue-at - \
        --output "${partial}" "${MODEL_RELEASE_URL}/${filename}"
    if [[ "$(stat --format='%s' "${partial}")" != "${expected_size}" ]]; then
        echo "UVR5 model download has an unexpected size: ${partial}" >&2
        return 1
    fi
    mv "${partial}" "${target}"
}

if [[ ! -f "${UVR5_MODEL_ROOT}/download_checks.json" ]]; then
    cp /usr/local/share/music-uvr5-api/model-catalog.json \
        "${UVR5_MODEL_ROOT}/download_checks.json"
fi

install_model_file "melband_roformer_big_beta4.ckpt" 1574477088
install_model_file "config_melbandroformer_big_beta4.yaml" 908
install_model_file "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt" 913107578
install_model_file "dereverb_mel_band_roformer_anvuew.yaml" 1846

exec python3 -m uvicorn verdantflare_uvr5.api:app \
    --host 0.0.0.0 --port 8000 --workers 1 "$@"
