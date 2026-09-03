#!/usr/bin/env bash
set -euo pipefail

readonly upstream_dir=/opt/minimax-h3-turbo
readonly model_path="${H3_MODEL_PATH:-/models/MiniMax-H3-Diffusers}"
readonly lora_path="${H3_LORA_PATH:-/models/MiniMax-H3-Turbo/minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors}"
readonly jobs_json="${H3_JOBS_JSON:-/inputs/ref2va.json}"
readonly output_dir="${H3_OUTPUT_DIR:-/outputs}"
readonly expected_lora_sha256=9e642fc8749c74f8da5e2382877ab5c7aa37b9a73b7fd0d6d457bd1b3cb1ae99
readonly model_revision=42ed227ee7df40d41602854ae760620d6eb651fe
readonly turbo_commit=02e26d591f7a04d5d1a074c9566d5dd4f22f6225
readonly lora_revision=05ef678438e84933c406131b59abbf86919b3aac

fail() {
    printf 'LightX2V benchmark: %s\n' "$*" >&2
    exit 1
}

[[ -f "${model_path}/modular_model_index.json" ]] || \
    fail "MiniMax H3 Diffusers model is incomplete at ${model_path}"
[[ -f "${model_path}/.verdantflare-model-revision" ]] || \
    fail "model revision marker is missing; run prepare-lightx2v-models"
[[ "$(<"${model_path}/.verdantflare-model-revision")" == "${model_revision}" ]] || \
    fail "model revision does not match ${model_revision}"
[[ -f "$(dirname "${lora_path}")/.verdantflare-model-revision" ]] || \
    fail "LoRA revision marker is missing; run prepare-lightx2v-models"
[[ "$(<"$(dirname "${lora_path}")/.verdantflare-model-revision")" == "${lora_revision}" ]] || \
    fail "LoRA revision does not match ${lora_revision}"
[[ -f "${lora_path}" ]] || fail "Ref2VA Turbo LoRA is missing at ${lora_path}"
[[ -f "${jobs_json}" ]] || fail "jobs JSON is missing at ${jobs_json}"
[[ -w "${output_dir}" ]] || fail "output directory is not writable: ${output_dir}"
command -v nvidia-smi >/dev/null || fail "nvidia-smi is unavailable"
command -v ffprobe >/dev/null || fail "ffprobe is unavailable"
command -v jq >/dev/null || fail "jq is unavailable"
command -v /usr/bin/time >/dev/null || fail "/usr/bin/time is unavailable"

jq -e '
    (.examples | type == "array" and length > 0) and
    all(.examples[];
        ((.task // "") | ascii_downcase) as $task |
        ($task == "ref2va" or $task == "ref2v") and
        (.references | type == "array" and length > 0)
    )
' "${jobs_json}" >/dev/null || fail "jobs JSON must contain only Ref2VA examples with references"

actual_lora_sha256="$(sha256sum "${lora_path}" | awk '{print $1}')"
[[ "${actual_lora_sha256}" == "${expected_lora_sha256}" ]] || \
    fail "unexpected LoRA SHA-256: ${actual_lora_sha256}"

run_id="$(date -u +%Y%m%dT%H%M%SZ)"
readonly run_id
readonly run_dir="${output_dir}/${run_id}"
readonly generated_dir="${run_dir}/generated"
mkdir -p "${generated_dir}"

readonly gpu_metrics="${run_dir}/gpu.csv"
readonly inference_log="${run_dir}/inference.log"
readonly resource_log="${run_dir}/resource-time.txt"
readonly media_json="${run_dir}/media.json"
readonly manifest_json="${run_dir}/manifest.json"
started_epoch="$(date +%s)"
readonly started_epoch
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
readonly started_at

nvidia-smi \
    --query-gpu=timestamp,index,uuid,name,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader,nounits \
    --loop=1 >"${gpu_metrics}" &
metrics_pid=$!
cleanup() {
    kill "${metrics_pid}" 2>/dev/null || true
    wait "${metrics_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

set +e
/usr/bin/time -v -o "${resource_log}" \
    python3 "${upstream_dir}/inference_minimax_h3.py" \
        --jobs-json "${jobs_json}" \
        --model-id "${model_path}" \
        --lora-path "${lora_path}" \
        --output-dir "${generated_dir}" \
        --seed 7 \
        --inference-steps 4 \
        --video-shift 12 \
        --audio-shift 3 \
        --reference-resize-mode match \
        --memory-reserve-margin 12GB \
        2>&1 | tee "${inference_log}"
inference_status=${PIPESTATUS[0]}
set -e

cleanup
trap - EXIT INT TERM

finished_epoch="$(date +%s)"
readonly finished_epoch
finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
readonly finished_at
readonly duration_seconds="$((finished_epoch - started_epoch))"

mapfile -d '' generated_files < <(find "${generated_dir}" -maxdepth 1 -type f -name '*.mp4' -print0 | sort -z)
if [[ ${#generated_files[@]} -gt 0 ]]; then
    ffprobe -v error -show_format -show_streams -of json "${generated_files[0]}" >"${media_json}"
else
    printf '{"streams":[],"format":null}\n' >"${media_json}"
fi

jq -n \
    --arg run_id "${run_id}" \
    --arg started_at "${started_at}" \
    --arg finished_at "${finished_at}" \
    --argjson duration_seconds "${duration_seconds}" \
    --argjson exit_code "${inference_status}" \
    --arg model_revision "${model_revision}" \
    --arg turbo_commit "${turbo_commit}" \
    --arg lora_revision "${lora_revision}" \
    --arg lora_sha256 "${actual_lora_sha256}" \
    --arg model_index_sha256 "$(sha256sum "${model_path}/modular_model_index.json" | awk '{print $1}')" \
    --arg jobs_sha256 "$(sha256sum "${jobs_json}" | awk '{print $1}')" \
    --argjson output_count "${#generated_files[@]}" \
    '{
        run_id: $run_id,
        started_at: $started_at,
        finished_at: $finished_at,
        duration_seconds: $duration_seconds,
        exit_code: $exit_code,
        model_revision: $model_revision,
        turbo_commit: $turbo_commit,
        lora_revision: $lora_revision,
        lora_sha256: $lora_sha256,
        model_index_sha256: $model_index_sha256,
        jobs_sha256: $jobs_sha256,
        parameters: {
            task: "ref2va",
            seed: 7,
            nfe: 4,
            video_shift: 12,
            audio_shift: 3,
            reference_resize_mode: "match",
            memory_reserve_margin: "12GB"
        },
        output_count: $output_count
    }' >"${manifest_json}"

if [[ "${inference_status}" -ne 0 ]]; then
    fail "inference failed with exit code ${inference_status}; see ${inference_log}"
fi
[[ ${#generated_files[@]} -gt 0 ]] || fail "inference completed without an MP4 output"

printf 'LightX2V benchmark result: %s\n' "${run_dir}"
