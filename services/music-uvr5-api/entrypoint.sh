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

readonly MODEL_RELEASE_URL="https://hf-mirror.com/Eddycrack864/audio-separator-models/resolve/785c7f7ec9dc7e9b0d0eb22616cdf4d00778a5b5/roformers"

install_model_file() {
    local filename="$1"
    local expected_size="$2"
    local expected_sha256="$3"
    local target="${UVR5_MODEL_ROOT}/${filename}"
    local partial="${target}.part"

    if [[ -f "${target}" ]]; then
        if [[ "$(stat --format='%s' "${target}")" != "${expected_size}" ]]; then
            echo "UVR5 model file has an unexpected size: ${target}" >&2
            return 1
        fi
        if [[ "$(sha256sum "${target}" | cut --delimiter=' ' --fields=1)" != "${expected_sha256}" ]]; then
            echo "UVR5 model file has an unexpected SHA-256: ${target}" >&2
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
    if [[ "$(sha256sum "${partial}" | cut --delimiter=' ' --fields=1)" != "${expected_sha256}" ]]; then
        echo "UVR5 model download has an unexpected SHA-256: ${partial}" >&2
        return 1
    fi
    mv "${partial}" "${target}"
}

if [[ ! -f "${UVR5_MODEL_ROOT}/download_checks.json" ]]; then
    cp /usr/local/share/music-uvr5-api/model-catalog.json \
        "${UVR5_MODEL_ROOT}/download_checks.json"
fi

install_model_file "melband_roformer_big_beta4.ckpt" 1574477088 \
    "700a9bd3831d4f7f44cc0019b238774e31045bcbc361fbb69235535c40fc1454"
install_model_file "config_melbandroformer_big_beta4.yaml" 908 \
    "464f21925d2744be6af64cf9ee78d5937d11cfde0b91f819cb1f631acccab603"
install_model_file "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt" 913107578 \
    "9262877b87e9ebb0fb808a456b0a411fa677f5df31c8383c1254af531c078970"
install_model_file "dereverb_mel_band_roformer_anvuew.yaml" 1846 \
    "1599d9ea717ea2b5b3bc55d936f752b8d0f67baaa3de95acd6d03259a2f37784"

exec python3 -m uvicorn verdantflare_uvr5.api:app \
    --host 0.0.0.0 --port 8000 --workers 1 "$@"
