#!/usr/bin/env bash
set -euo pipefail

readonly benchmark_root="${H3_BENCHMARK_OUTPUT:?H3_BENCHMARK_OUTPUT is required}"
readonly benchmark_cases="${H3_BENCHMARK_CASES:?H3_BENCHMARK_CASES is required}"
readonly server_port="${H3_SERVER_PORT:-8000}"
readonly base_url="http://127.0.0.1:${server_port}"
readonly reference_url="${H3_REFERENCE_VIDEO_URL:-https://cdn.hailuoai.com/prod/hailuo_demo/testsets/h3_promo_eval_ref2va/gallery/sr_v2p26_trio_seed42_20260724/inputs/297573323635_00_%E8%A7%86%E9%A2%911_YnyRbxEwio_video_20260525_163755_1927e9d3.mp4}"
readonly prompt="${H3_BENCHMARK_PROMPT:-Use <Video 1> as the subject and motion reference. Preserve the person and pastoral setting with subtle natural movement and coherent ambient sound.}"
readonly duration_seconds="${H3_DURATION_SECONDS:-4}"
readonly short_edge="${H3_SHORT_EDGE:-768}"
readonly aspect_ratio="${H3_ASPECT_RATIO:-9:16}"
readonly warmup_seed="${H3_WARMUP_SEED:-0}"
readonly timed_seed="${H3_TIMED_SEED:-7}"
readonly poll_interval_seconds="${H3_POLL_INTERVAL_SECONDS:-5}"
readonly max_poll_count="${H3_MAX_POLL_COUNT:-4320}"
readonly gpu_quiesce_timeout_seconds="${H3_GPU_QUIESCE_TIMEOUT_SECONDS:-300}"
readonly gpu_quiesce_poll_seconds="${H3_GPU_QUIESCE_POLL_SECONDS:-5}"

if [[ -e "${benchmark_root}" ]]; then
    echo "Benchmark output already exists: ${benchmark_root}" >&2
    exit 1
fi
mkdir -p "${benchmark_root}/server-output"
readonly reference_file="${benchmark_root}/reference.mp4"

curl --fail --location --silent --show-error "${reference_url}" --output "${reference_file}"
ffprobe -v error -show_entries stream=codec_type -of json "${reference_file}" \
    >"${benchmark_root}/reference.ffprobe.json"
sha256sum "${reference_file}" >"${benchmark_root}/reference.sha256"

gpu_quiesce_deadline=$((SECONDS + gpu_quiesce_timeout_seconds))
while true; do
    gpu_process_output="$(
        nvidia-smi --query-compute-apps=pid,used_memory \
            --format=csv,noheader,nounits
    )"
    mapfile -t gpu_processes < <(
        printf '%s\n' "${gpu_process_output}" | sed '/^[[:space:]]*$/d'
    )
    if (( ${#gpu_processes[@]} == 0 )); then
        break
    fi
    if (( SECONDS >= gpu_quiesce_deadline )); then
        echo "GPU still has external CUDA processes after ${gpu_quiesce_timeout_seconds}s:" >&2
        printf '  %s\n' "${gpu_processes[@]}" >&2
        exit 1
    fi
    echo "Waiting for external CUDA processes to exit: ${gpu_processes[*]}"
    sleep "${gpu_quiesce_poll_seconds}"
done

export H3_SERVER_OUTPUT_PATH="${benchmark_root}/server-output"
music-minimax-h3-sglang-server >"${benchmark_root}/server.log" 2>&1 &
server_pid=$!
monitor_pid=""

cleanup() {
    if [[ -n "${monitor_pid}" ]]; then
        kill "${monitor_pid}" 2>/dev/null || true
        wait "${monitor_pid}" 2>/dev/null || true
    fi
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
}
trap cleanup EXIT

for _ in $(seq 1 2160); do
    if curl --fail --silent --show-error "${base_url}/health" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        echo "SGLang exited before becoming healthy." >&2
        tail -200 "${benchmark_root}/server.log" >&2
        exit 1
    fi
    sleep 10
done
curl --fail --silent --show-error "${base_url}/health" >/dev/null

(
    while kill -0 "${server_pid}" 2>/dev/null; do
        timestamp="$(date --iso-8601=ns)"
        nvidia-smi \
            --query-gpu=timestamp,uuid,memory.used,memory.total,utilization.gpu,power.draw \
            --format=csv,noheader,nounits | sed "s/^/${timestamp},/"
        sleep 1
    done
) >"${benchmark_root}/gpu.csv" &
monitor_pid=$!

submit_run() {
    local case_name="$1"
    local run_name="$2"
    local sigma_points="$3"
    local seed="$4"
    local run_dir="${benchmark_root}/${case_name}/${run_name}"
    local request_file="${run_dir}/request.json"
    local create_file="${run_dir}/create.json"
    local status_file="${run_dir}/status.json"
    local video_file="${run_dir}/output.mp4"
    local video_id status start_ns end_ns elapsed_seconds

    mkdir -p "${run_dir}"
    jq -n \
        --arg model "MiniMaxAI/MiniMax-H3" \
        --arg prompt "${prompt}" \
        --arg uri "file://${reference_file}" \
        --arg aspect_ratio "${aspect_ratio}" \
        --argjson seconds "${duration_seconds}" \
        --argjson short_edge "${short_edge}" \
        --argjson sigma_points "${sigma_points}" \
        --argjson seed "${seed}" \
        '{
          model: $model,
          task: "ref2va",
          prompt: $prompt,
          seconds: $seconds,
          conditions: [{type: "video", uri: $uri, role: "reference"}],
          target: {
            short_edge: $short_edge,
            aspect_ratio: $aspect_ratio,
            duration_seconds: $seconds
          },
          num_outputs_per_prompt: 1,
          num_inference_steps: $sigma_points,
          flow_shift: 12.0,
          audio_flow_shift: 3.0,
          seed: $seed
        }' >"${request_file}"

    start_ns="$(date +%s%N)"
    curl --fail-with-body --silent --show-error \
        --request POST \
        --url "${base_url}/v1/videos" \
        --header 'Content-Type: application/json' \
        --data-binary "@${request_file}" >"${create_file}"
    video_id="$(jq -er '.id' "${create_file}")"

    status=""
    for _ in $(seq 1 "${max_poll_count}"); do
        curl --fail --silent --show-error \
            "${base_url}/v1/videos/${video_id}" >"${status_file}"
        status="$(jq -er '.status' "${status_file}")"
        case "${status}" in
            completed)
                break
                ;;
            failed|failure)
                echo "MiniMax H3 benchmark failed: ${case_name}/${run_name}" >&2
                return 1
                ;;
            queued|in_progress)
                sleep "${poll_interval_seconds}"
                ;;
            *)
                echo "Unknown MiniMax H3 status: ${status}" >&2
                return 1
                ;;
        esac
    done
    if [[ "${status}" != "completed" ]]; then
        echo "MiniMax H3 benchmark timed out: ${case_name}/${run_name}" >&2
        return 1
    fi
    end_ns="$(date +%s%N)"

    curl --fail --silent --show-error \
        "${base_url}/v1/videos/${video_id}/content" --output "${video_file}"
    ffprobe -v error -show_streams -show_format -of json "${video_file}" \
        >"${run_dir}/ffprobe.json"
    elapsed_seconds="$(awk -v start="${start_ns}" -v end="${end_ns}" \
        'BEGIN { printf "%.3f", (end - start) / 1000000000 }')"
    jq -n \
        --arg case_name "${case_name}" \
        --arg run_name "${run_name}" \
        --arg video_id "${video_id}" \
        --arg quantization "${H3_QUANTIZATION:-bf16}" \
        --arg transformer_weights_path "${H3_TRANSFORMER_WEIGHTS_PATH:-}" \
        --arg sglang_commit "${SGLANG_COMMIT:-unknown}" \
        --arg comfy_kitchen_version "${COMFY_KITCHEN_VERSION:-unknown}" \
        --argjson sigma_points "${sigma_points}" \
        --argjson nfe "$((sigma_points - 1))" \
        --argjson seed "${seed}" \
        --argjson elapsed_seconds "${elapsed_seconds}" \
        '{
          case: $case_name,
          run: $run_name,
          video_id: $video_id,
          quantization: $quantization,
          transformer_weights_path: $transformer_weights_path,
          sglang_commit: $sglang_commit,
          comfy_kitchen_version: $comfy_kitchen_version,
          sigma_points: $sigma_points,
          nfe: $nfe,
          seed: $seed,
          elapsed_seconds: $elapsed_seconds
        }' >"${run_dir}/result.json"
}

IFS=',' read -r -a cases <<<"${benchmark_cases}"
for benchmark_case in "${cases[@]}"; do
    case_name="${benchmark_case%%:*}"
    sigma_points="${benchmark_case##*:}"
    if [[ -z "${case_name}" || ! "${sigma_points}" =~ ^[2-9][0-9]*$ ]]; then
        echo "Invalid H3_BENCHMARK_CASES entry: ${benchmark_case}" >&2
        exit 1
    fi
    submit_run "${case_name}" warmup "${sigma_points}" "${warmup_seed}"
    submit_run "${case_name}" timed "${sigma_points}" "${timed_seed}"
done

if [[ -r /sys/fs/cgroup/memory.peak ]]; then
    cp /sys/fs/cgroup/memory.peak "${benchmark_root}/cgroup-memory.peak"
fi
sync
touch "${benchmark_root}/COMPLETED"
