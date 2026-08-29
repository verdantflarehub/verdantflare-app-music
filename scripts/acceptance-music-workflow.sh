#!/usr/bin/env bash
set -euo pipefail

readonly PLAN_FILE="${PLAN_FILE:?PLAN_FILE is required}"
readonly LYRICS_FILE="${LYRICS_FILE:?LYRICS_FILE is required}"
readonly ALIGNMENT_LYRICS_FILE="${ALIGNMENT_LYRICS_FILE:?ALIGNMENT_LYRICS_FILE is required}"
readonly INSTRUCTIONS_FILE="${INSTRUCTIONS_FILE:?INSTRUCTIONS_FILE is required}"
readonly USER_VOICE_FILE="${USER_VOICE_FILE:?USER_VOICE_FILE is required}"
readonly OUTPUT_DIR="${OUTPUT_DIR:?OUTPUT_DIR is required and must not exist}"
readonly BPM="${BPM:?BPM is required}"
readonly MODEL_ID="${MODEL_ID:-TonyStark}"
readonly SELECTED_CANDIDATE="${SELECTED_CANDIDATE:-1}"
readonly MUSIC3_URL="${MUSIC3_URL:-http://127.0.0.1:8001}"
readonly UVR5_URL="${UVR5_URL:-http://127.0.0.1:8002}"
readonly RVC_URL="${RVC_URL:-http://127.0.0.1:8003}"
readonly LYRICS_ALIGNER_URL="${LYRICS_ALIGNER_URL:-http://127.0.0.1:8004}"
readonly MIXER_URL="${MIXER_URL:-http://127.0.0.1:8005}"
readonly SEED="${SEED:-7}"
readonly MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-9000}"

if [[ "${SELECTED_CANDIDATE}" != "1" && "${SELECTED_CANDIDATE}" != "2" ]]; then
    echo "SELECTED_CANDIDATE must be 1 or 2" >&2
    exit 1
fi
for input in "${PLAN_FILE}" "${LYRICS_FILE}" "${INSTRUCTIONS_FILE}" \
    "${ALIGNMENT_LYRICS_FILE}" "${USER_VOICE_FILE}"; do
    test -s "${input}"
done
if [[ -e "${OUTPUT_DIR}" ]]; then
    echo "OUTPUT_DIR already exists: ${OUTPUT_DIR}" >&2
    exit 1
fi
mkdir -p "${OUTPUT_DIR}/.work"
readonly WORK_DIR="${OUTPUT_DIR}/.work"

for url in "${MUSIC3_URL}" "${UVR5_URL}" "${RVC_URL}" \
    "${LYRICS_ALIGNER_URL}" "${MIXER_URL}"; do
    curl --fail --silent --show-error "${url}/health" >/dev/null
done

cp "${PLAN_FILE}" "${OUTPUT_DIR}/完整词曲企划.md"

generate_candidate() {
    local number="$1"
    local candidate_seed=$((SEED + number - 1))
    local request="${WORK_DIR}/candidate-${number}.json"
    local wav="${WORK_DIR}/candidate-${number}.wav"
    python3 - "${LYRICS_FILE}" "${INSTRUCTIONS_FILE}" "${candidate_seed}" \
        "${MAX_NEW_TOKENS}" > "${request}" <<'PY'
import json
import sys

lyrics_path, instructions_path, seed, tokens = sys.argv[1:]
print(json.dumps({
    "model": "MiniMaxAI/MiniMax-Music3",
    "input": open(lyrics_path, encoding="utf-8").read(),
    "instructions": open(instructions_path, encoding="utf-8").read(),
    "seed": int(seed),
    "max_new_tokens": int(tokens),
    "response_format": "wav",
    "stream": False,
}))
PY
    curl --fail --silent --show-error \
        -H "Content-Type: application/json" \
        --data-binary "@${request}" \
        "${MUSIC3_URL}/v1/audio/speech" --output "${wav}"
    ffmpeg -v error -y -i "${wav}" -codec:a libmp3lame -b:a 320k \
        "${OUTPUT_DIR}/Demo_Candidate_${number}.mp3"
}

generate_candidate 1
generate_candidate 2
cp "${OUTPUT_DIR}/Demo_Candidate_${SELECTED_CANDIDATE}.mp3" \
    "${OUTPUT_DIR}/Demo_Selected.mp3"

curl --fail --silent --show-error \
    --form "audio=@${OUTPUT_DIR}/Demo_Selected.mp3" \
    "${UVR5_URL}/v1/audio/stem-separations" \
    --output "${WORK_DIR}/stems.zip"

curl --fail --silent --show-error \
    --form "audio=@${USER_VOICE_FILE}" \
    --form "model_id=${MODEL_ID}" \
    --form "epochs=${RVC_EPOCHS:-200}" \
    --form "batch_size=${RVC_BATCH_SIZE:-4}" \
    --form "save_every_epochs=${RVC_SAVE_EVERY_EPOCHS:-50}" \
    "${RVC_URL}/v1/voice-models/train" \
    --output "${WORK_DIR}/voice-model.zip"

python3 - "${OUTPUT_DIR}" "${WORK_DIR}/stems.zip" "${WORK_DIR}/voice-model.zip" <<'PY'
import pathlib
import sys
import zipfile

output = pathlib.Path(sys.argv[1]).resolve()
for archive_name in sys.argv[2:]:
    with zipfile.ZipFile(archive_name) as archive:
        for item in archive.infolist():
            if item.filename == "manifest.json":
                continue
            target = (output / item.filename).resolve()
            if target.parent != output:
                raise SystemExit(f"unsafe archive member: {item.filename}")
            target.write_bytes(archive.read(item))
PY

curl --fail --silent --show-error \
    --form "audio=@${OUTPUT_DIR}/vocal_dry_original.wav" \
    --form "model_id=${MODEL_ID}" \
    --form "f0_method=rmvpe" \
    "${RVC_URL}/v1/audio/voice-conversions" \
    --output "${OUTPUT_DIR}/vocal_dry_cloned.wav"

curl --fail --silent --show-error \
    --form "audio=@${OUTPUT_DIR}/vocal_dry_cloned.wav" \
    --form "lyrics=@${ALIGNMENT_LYRICS_FILE};type=text/plain;charset=utf-8" \
    --form "language=zh" \
    "${LYRICS_ALIGNER_URL}/v1/lyrics/alignments" \
    --output "${OUTPUT_DIR}/Aligned_Lyrics.lrc"

curl --fail --silent --show-error \
    --form "instrumental=@${OUTPUT_DIR}/instrumental.wav" \
    --form "vocal=@${OUTPUT_DIR}/vocal_dry_cloned.wav" \
    --form "lyrics_lrc=@${OUTPUT_DIR}/Aligned_Lyrics.lrc" \
    --form "bpm=${BPM}" \
    "${MIXER_URL}/v1/audio/masters" \
    --output "${WORK_DIR}/final-song.zip"

python3 - "${OUTPUT_DIR}" "${WORK_DIR}/final-song.zip" <<'PY'
import pathlib
import sys
import zipfile

output = pathlib.Path(sys.argv[1]).resolve()
with zipfile.ZipFile(sys.argv[2]) as archive:
    for item in archive.infolist():
        if item.filename == "manifest.json":
            continue
        target = (output / item.filename).resolve()
        if target.parent != output:
            raise SystemExit(f"unsafe archive member: {item.filename}")
        target.write_bytes(archive.read(item))
PY

python3 - "${OUTPUT_DIR}" "${MODEL_ID}" "${ALIGNMENT_LYRICS_FILE}" <<'PY'
import json
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
model_id = sys.argv[2]
expected = [
    "完整词曲企划.md",
    "Demo_Candidate_1.mp3",
    "Demo_Candidate_2.mp3",
    "Demo_Selected.mp3",
    "instrumental.wav",
    "vocal_dry_original.wav",
    f"{model_id}.pth",
    f"{model_id}.index",
    f"{model_id}_validation.wav",
    "vocal_dry_cloned.wav",
    "Aligned_Lyrics.lrc",
    "Final_Song_Master.wav",
    "Final_Song.mp3",
    "Final_Song.lrc",
]
for name in expected:
    path = root / name
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"missing or empty output: {name}")

def probe(name):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(root / name)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)

for name in ("instrumental.wav", "vocal_dry_original.wav", "Final_Song_Master.wav"):
    stream = probe(name)["streams"][0]
    if int(stream["sample_rate"]) != 48000:
        raise SystemExit(f"unexpected sample rate: {name}")
if probe("Final_Song_Master.wav")["streams"][0].get("bits_per_raw_sample") != "24":
    raise SystemExit("Final_Song_Master.wav is not 24-bit")
duration = float(probe(f"{model_id}_validation.wav")["format"]["duration"])
if duration < 14.5 or duration > 15.1:
    raise SystemExit("model validation audio is not 15 seconds")
if (root / "Final_Song.lrc").read_bytes() != (root / "Aligned_Lyrics.lrc").read_bytes():
    raise SystemExit("Final_Song.lrc differs from aligned LRC")

source_lines = [line.strip() for line in pathlib.Path(sys.argv[3]).read_text(encoding="utf-8").splitlines() if line.strip()]
aligned_lines = [line for line in (root / "Aligned_Lyrics.lrc").read_text(encoding="utf-8").splitlines() if line]
if len(aligned_lines) != len(source_lines):
    raise SystemExit("Aligned_Lyrics.lrc changed the lyric line count")
if [line.split("]", 1)[1] for line in aligned_lines] != source_lines:
    raise SystemExit("Aligned_Lyrics.lrc changed the approved lyrics")

timestamp_pattern = re.compile(r"^\[(\d{1,3}):(\d{2})\.(\d{3})\]")
timestamps = []
for line in aligned_lines:
    match = timestamp_pattern.match(line)
    if match is None or int(match.group(2)) >= 60:
        raise SystemExit("Aligned_Lyrics.lrc contains an invalid timestamp")
    timestamps.append((int(match.group(1)) * 60 + int(match.group(2))) * 1000 + int(match.group(3)))
if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
    raise SystemExit("Aligned_Lyrics.lrc timestamps are not strictly increasing")
vocal_duration_ms = round(float(probe("vocal_dry_cloned.wav")["format"]["duration"]) * 1000)
if timestamps[-1] > vocal_duration_ms:
    raise SystemExit("Aligned_Lyrics.lrc exceeds the vocal duration")
print(json.dumps({"status": "passed", "outputs": expected}, ensure_ascii=False, indent=2))
PY

rm -rf "${WORK_DIR}"
