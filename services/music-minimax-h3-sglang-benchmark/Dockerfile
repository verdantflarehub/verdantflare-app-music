# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=registry.cn-qingdao.aliyuncs.com/wod/verdantflare-app:music-minimax-h3-api-v0.1.1
FROM ${BASE_IMAGE}

ARG SGLANG_COMMIT=bbbcbf9418f0d8fbea968d96f3b470f5b883bac3
ARG COMFY_KITCHEN_VERSION=0.2.31
ARG BENCHMARK_FLASHINFER_VERSION=0.6.17

USER root

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare MiniMax H3 SGLang RTX 4090 benchmark"
LABEL org.opencontainers.image.version="0.1.7"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-MiniMax-H3-Community"
LABEL org.opencontainers.image.sglang.revision="${SGLANG_COMMIT}"

# The released v0.1.1 service image supplies the CUDA 13 runtime and the
# v0.5.18 build dependencies already validated on the target cluster.
# Replace its source tree with the revision that introduced the official RTX
# 4090 measurements, then lock the optional INT8 kernel dependency.
RUN git -C /sgl-workspace/sglang fetch --depth=1 \
        https://github.com/sgl-project/sglang.git "${SGLANG_COMMIT}" && \
    git -C /sgl-workspace/sglang checkout --detach --force FETCH_HEAD && \
    test "$(git -C /sgl-workspace/sglang rev-parse HEAD)" = "${SGLANG_COMMIT}" && \
    test -z "$(git -C /sgl-workspace/sglang status --short)" && \
    python3 -m pip install --no-cache-dir -e "/sgl-workspace/sglang/python[diffusion]" && \
    python3 -m pip uninstall --yes flashinfer-cubin flashinfer-jit-cache && \
    python3 -m pip install --no-cache-dir \
        "flashinfer-cubin==${BENCHMARK_FLASHINFER_VERSION}" \
        --index-url https://flashinfer.ai/whl && \
    python3 -c "from importlib.metadata import version; expected='${BENCHMARK_FLASHINFER_VERSION}'; assert version('flashinfer-python') == expected; assert version('flashinfer-cubin') == expected; import flashinfer" && \
    python3 -m pip install --no-cache-dir "comfy-kitchen==${COMFY_KITCHEN_VERSION}"

RUN apt-get update && \
    apt-get install --yes --no-install-recommends jq && \
    rm -rf /var/lib/apt/lists/*

COPY server-entrypoint.sh /usr/local/bin/music-minimax-h3-sglang-server
COPY run-benchmark.sh /usr/local/bin/music-minimax-h3-sglang-benchmark
COPY NOTICE.upstream.md /usr/share/licenses/music-minimax-h3-sglang-benchmark/NOTICE.upstream.md
RUN chmod 0755 \
    /usr/local/bin/music-minimax-h3-sglang-server \
    /usr/local/bin/music-minimax-h3-sglang-benchmark

ENV MODEL_PATH=/models/MiniMax-H3 \
    HF_HOME=/models/.cache/huggingface \
    H3_QUANTIZATION=bf16 \
    H3_SERVER_PORT=8000 \
    SGLANG_COMMIT=${SGLANG_COMMIT} \
    COMFY_KITCHEN_VERSION=${COMFY_KITCHEN_VERSION} \
    FLASHINFER_VERSION=${BENCHMARK_FLASHINFER_VERSION}

VOLUME ["/models/MiniMax-H3", "/data/projects"]

ENTRYPOINT ["/usr/local/bin/music-minimax-h3-sglang-benchmark"]
