# syntax=docker/dockerfile:1.7

FROM lmsysorg/sglang:v0.5.18-cu130

USER root

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare MiniMax H3 Ref2VA API"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-MiniMax-H3-Community"

# The tagged SGLang image contains the matching source tree. Install its
# diffusion extras without following a moving Git branch.
RUN python3 -m pip install --no-cache-dir -e "/sgl-workspace/sglang/python[diffusion]"; \
    python3 -c "import sglang; assert sglang.__version__ == '0.5.18'"

COPY entrypoint.sh /usr/local/bin/music-minimax-h3-entrypoint
COPY smoke-test.sh /usr/local/bin/music-minimax-h3-smoke-test
COPY NOTICE.upstream.md /usr/share/licenses/music-minimax-h3-api/NOTICE.upstream.md
RUN chmod 0755 \
    /usr/local/bin/music-minimax-h3-entrypoint \
    /usr/local/bin/music-minimax-h3-smoke-test

ENV MODEL_PATH=/models/MiniMax-H3 \
    HF_HOME=/models/.cache/huggingface \
    H3_VISIBLE_GPU_COUNT=8

VOLUME ["/models/MiniMax-H3", "/data/projects"]

EXPOSE 8000

HEALTHCHECK --start-period=24h --interval=30s --timeout=5s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/music-minimax-h3-entrypoint"]
