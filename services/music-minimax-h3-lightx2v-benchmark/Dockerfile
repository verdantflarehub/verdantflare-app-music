# syntax=docker/dockerfile:1.7

FROM lmsysorg/sglang:v0.5.18-cu130

USER root

ARG MINIMAX_H3_TURBO_COMMIT=02e26d591f7a04d5d1a074c9566d5dd4f22f6225

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare MiniMax H3 LightX2V Benchmark"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-MiniMax-H3-Community"

# This is a separate benchmark image. SGLang 0.5.18 pins Diffusers 0.37.0,
# while released MiniMax H3 modular pipelines require Diffusers 0.40.0.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends time; \
    rm -rf /var/lib/apt/lists/*; \
    git clone --filter=blob:none https://github.com/ModelTC/Minimax-H3-Turbo.git /opt/minimax-h3-turbo; \
    git -C /opt/minimax-h3-turbo checkout "${MINIMAX_H3_TURBO_COMMIT}"; \
    python3 -m pip install --no-cache-dir \
        "diffusers==0.40.0" \
        "peft>=0.18.0,<0.19"; \
    python3 -c "import diffusers; assert diffusers.__version__ == '0.40.0'"; \
    python3 -c "from diffusers.modular_pipelines.minimax_h3 import MiniMaxH3ImageReference"; \
    test "$(git -C /opt/minimax-h3-turbo rev-parse HEAD)" = "${MINIMAX_H3_TURBO_COMMIT}"

COPY run-ref2va-benchmark.sh /usr/local/bin/run-ref2va-benchmark
COPY prepare-models.sh /usr/local/bin/prepare-lightx2v-models
COPY NOTICE.upstream.md /usr/share/licenses/music-minimax-h3-lightx2v-benchmark/NOTICE.upstream.md
RUN chmod 0755 \
    /usr/local/bin/run-ref2va-benchmark \
    /usr/local/bin/prepare-lightx2v-models

ENV H3_MODEL_PATH=/models/MiniMax-H3-Diffusers \
    H3_LORA_PATH=/models/MiniMax-H3-Turbo/minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors \
    H3_JOBS_JSON=/inputs/ref2va.json \
    H3_OUTPUT_DIR=/outputs

VOLUME ["/models/MiniMax-H3-Diffusers", "/models/MiniMax-H3-Turbo", "/inputs", "/outputs"]

ENTRYPOINT ["/opt/nvidia/nvidia_entrypoint.sh", "/usr/local/bin/run-ref2va-benchmark"]
