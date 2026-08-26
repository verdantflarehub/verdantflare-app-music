#!/usr/bin/env bash
set -euo pipefail

readonly BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
readonly OUTPUT_FILE="${OUTPUT_FILE:-/tmp/minimax-music3-smoke.wav}"

curl --fail --silent --show-error "${BASE_URL}/health" >/dev/null

curl --fail --silent --show-error \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{
      "model": "MiniMaxAI/MiniMax-Music3",
      "input": "[Verse]\nMorning light across the open road\n[Chorus]\nWe carry on, we carry home",
      "instructions": "A warm acoustic pop song at 92 BPM with intimate vocals, fingerpicked guitar, soft piano, and a natural room sound.",
      "seed": 7,
      "max_new_tokens": 250,
      "response_format": "wav",
      "stream": false
    }' \
    "${BASE_URL}/v1/audio/speech" \
    --output "${OUTPUT_FILE}"

python3 - "${OUTPUT_FILE}" <<'PY'
import sys
import wave

with wave.open(sys.argv[1], "rb") as audio:
    assert audio.getnchannels() == 2, audio.getnchannels()
    assert audio.getframerate() == 32000, audio.getframerate()
    assert audio.getsampwidth() == 2, audio.getsampwidth()
    assert audio.getnframes() > 0

print(sys.argv[1])
PY
