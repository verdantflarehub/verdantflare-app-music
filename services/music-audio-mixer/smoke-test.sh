#!/usr/bin/env bash
set -euo pipefail

readonly BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
readonly INSTRUMENTAL_FILE="${INSTRUMENTAL_FILE:?INSTRUMENTAL_FILE is required}"
readonly VOCAL_FILE="${VOCAL_FILE:?VOCAL_FILE is required}"
readonly LRC_FILE="${LRC_FILE:?LRC_FILE is required}"
readonly BPM="${BPM:?BPM is required}"
readonly OUTPUT_FILE="${OUTPUT_FILE:-/tmp/Final_Song.zip}"

curl --fail --silent --show-error "${BASE_URL}/health" >/dev/null
curl --fail --silent --show-error \
    --form "instrumental=@${INSTRUMENTAL_FILE}" \
    --form "vocal=@${VOCAL_FILE}" \
    --form "lyrics_lrc=@${LRC_FILE}" \
    --form "bpm=${BPM}" \
    "${BASE_URL}/v1/audio/masters" --output "${OUTPUT_FILE}"
python3 - "${OUTPUT_FILE}" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    expected = {"Final_Song_Master.wav", "Final_Song.mp3", "Final_Song.lrc", "manifest.json"}
    assert expected == set(archive.namelist())
print(sys.argv[1])
PY
