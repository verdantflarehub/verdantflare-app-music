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
exec python3 -m uvicorn verdantflare_uvr5.api:app \
    --host 0.0.0.0 --port 8000 --workers 1 "$@"
