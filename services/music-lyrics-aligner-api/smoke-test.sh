#!/usr/bin/env bash
set -euo pipefail

readonly BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
readonly VOCAL_FILE="${VOCAL_FILE:?VOCAL_FILE is required}"
readonly LYRICS_FILE="${LYRICS_FILE:?LYRICS_FILE is required}"
readonly OUTPUT_FILE="${OUTPUT_FILE:-/tmp/Aligned_Lyrics.lrc}"

curl --fail --silent --show-error "${BASE_URL}/health" >/dev/null
curl --fail --silent --show-error \
    --form "audio=@${VOCAL_FILE}" \
    --form "lyrics=@${LYRICS_FILE};type=text/plain;charset=utf-8" \
    --form "language=zh" \
    "${BASE_URL}/v1/lyrics/alignments" --output "${OUTPUT_FILE}"
test -s "${OUTPUT_FILE}"
awk '/^\[[0-9]{2,3}:[0-9]{2}\.[0-9]{3}\].+$/ { count++ } END { exit count > 0 ? 0 : 1 }' "${OUTPUT_FILE}"
echo "${OUTPUT_FILE}"
