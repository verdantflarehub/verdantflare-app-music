# syntax=docker/dockerfile:1.7

FROM lmsysorg/sglang:v0.5.16-cu130

USER root

ARG UCX_COMMIT=d8e50df6651b9ea5b76f23aee0aefbf053a4137a
ARG SGLANG_OMNI_VERSION=0.1.3

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare MiniMax Music 3 API"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-MiniMax-Music3-Community"

# SGLang-Omni uses UCX for CUDA-aware tensor transport between pipeline stages.
RUN set -eux; \
    git clone --filter=blob:none https://github.com/openucx/ucx.git /tmp/ucx; \
    git -C /tmp/ucx checkout "${UCX_COMMIT}"; \
    cd /tmp/ucx; \
    ./autogen.sh; \
    ./contrib/configure-release-mt \
        --enable-shared \
        --disable-static \
        --disable-doxygen-doc \
        --enable-optimizations \
        --enable-cma \
        --enable-devel-headers \
        --with-cuda=/usr/local/cuda \
        --with-verbs \
        --with-dm \
        --prefix=/usr/local; \
    make -j"$(nproc)"; \
    make install-strip; \
    ldconfig; \
    rm -rf /tmp/ucx

RUN uv pip uninstall --system --break-system-packages flashinfer-cubin; \
    uv pip install --system --break-system-packages --prerelease=allow \
        "sglang-omni==${SGLANG_OMNI_VERSION}"; \
    python3 -c "import sglang_omni; assert sglang_omni.__version__ == '${SGLANG_OMNI_VERSION}'"

COPY entrypoint.sh /usr/local/bin/music-minimax-music3-entrypoint
COPY LICENSE.minimax-music3 /usr/share/licenses/music-minimax-music3-api/LICENSE.minimax-music3
COPY NOTICE.upstream.md /usr/share/licenses/music-minimax-music3-api/NOTICE.upstream.md
RUN chmod 0755 /usr/local/bin/music-minimax-music3-entrypoint

ENV MODEL_PATH=/models/MiniMax-Music3 \
    SGLANG_OMNI_STRICT_PORT=1 \
    SGLANG_OMNI_STARTUP_TIMEOUT=1800

VOLUME ["/models/MiniMax-Music3"]

EXPOSE 8000

HEALTHCHECK --start-period=30m --interval=30s --timeout=5s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/health >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/music-minimax-music3-entrypoint"]
