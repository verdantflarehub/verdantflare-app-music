#!/usr/bin/env bash
set -euo pipefail

readonly BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
readonly INPUT_FILE="${INPUT_FILE:?INPUT_FILE is required}"
readonly OUTPUT_FILE="${OUTPUT_FILE:-/tmp/stems.zip}"

curl --fail --silent --show-error "${BASE_URL}/health" >/dev/null
curl --fail --silent --show-error --form "audio=@${INPUT_FILE}" \
    "${BASE_URL}/v1/audio/stem-separations" --output "${OUTPUT_FILE}"
python3 - "${OUTPUT_FILE}" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    assert {"instrumental.wav", "vocal_dry_original.wav", "manifest.json"} == set(archive.namelist())
print(sys.argv[1])
PY
