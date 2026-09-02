# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=registry.cn-qingdao.aliyuncs.com/wod/verdantflare-app:music-minimax-h3-sglang-benchmark-v0.1.5
FROM ${BASE_IMAGE}

USER root

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare MiniMax H3 Ref2VA Full INT8 API"
LABEL org.opencontainers.image.version="0.2.0"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-MiniMax-H3-Community"

# Promote the exact SGLang/FlashInfer/comfy-kitchen image validated by the
# serialized Full INT8 benchmark; only the long-running service entrypoint differs.
COPY entrypoint.sh /usr/local/bin/music-minimax-h3-entrypoint
COPY smoke-test.sh /usr/local/bin/music-minimax-h3-smoke-test
COPY NOTICE.upstream.md /usr/share/licenses/music-minimax-h3-api/NOTICE.upstream.md
RUN chmod 0755 \
    /usr/local/bin/music-minimax-h3-entrypoint \
    /usr/local/bin/music-minimax-h3-smoke-test

ENV MODEL_PATH=/models/MiniMax-H3 \
    HF_HOME=/models/.cache/huggingface \
    H3_VISIBLE_GPU_COUNT=1

VOLUME ["/models/MiniMax-H3", "/data/projects"]

EXPOSE 8000

HEALTHCHECK --start-period=24h --interval=30s --timeout=5s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/music-minimax-h3-entrypoint"]
