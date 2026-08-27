#!/usr/bin/env bash
set -euo pipefail

readonly BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
readonly MODEL_ID="${MODEL_ID:?MODEL_ID is required}"
readonly INPUT_FILE="${INPUT_FILE:?INPUT_FILE is required}"
readonly OUTPUT_FILE="${OUTPUT_FILE:-/tmp/rvc-converted.wav}"

curl --fail --silent --show-error "${BASE_URL}/health" >/dev/null

models="$(curl --fail --silent --show-error "${BASE_URL}/v1/voice-models")"
python3 - "${MODEL_ID}" "${models}" <<'PY'
import json
import sys

model_id = sys.argv[1]
payload = json.loads(sys.argv[2])
assert model_id in {model["id"] for model in payload["data"]}, payload
PY

curl --fail --silent --show-error \
    --request POST \
    --form "audio=@${INPUT_FILE}" \
    --form "model_id=${MODEL_ID}" \
    --form "f0_method=rmvpe" \
    "${BASE_URL}/v1/audio/voice-conversions" \
    --output "${OUTPUT_FILE}"

python3 - "${OUTPUT_FILE}" <<'PY'
import sys
import wave

with wave.open(sys.argv[1], "rb") as audio:
    assert audio.getnchannels() in (1, 2), audio.getnchannels()
    assert audio.getframerate() >= 16000, audio.getframerate()
    assert audio.getsampwidth() == 2, audio.getsampwidth()
    assert audio.getnframes() > 0

print(sys.argv[1])
PY
