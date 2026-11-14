#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${MIXER_TEMP_ROOT}"
exec python3 -m uvicorn verdantflare_mixer.api:app \
    --host 0.0.0.0 --port 8000 --workers 1 "$@"
