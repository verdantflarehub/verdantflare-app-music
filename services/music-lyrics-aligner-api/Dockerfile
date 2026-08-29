ARG BASE_IMAGE=docker.io/pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
FROM ${BASE_IMAGE}

USER root
ARG DEBIAN_FRONTEND=noninteractive
ARG PYPI_INDEX_URL=https://pypi.org/simple

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare Music Lyrics Aligner API"
LABEL org.opencontainers.image.version="0.1.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" -r requirements.txt \
    && pip check
COPY NOTICE.upstream.md /usr/share/licenses/music-lyrics-aligner-api/NOTICE.upstream.md
COPY verdantflare_lyrics_aligner ./verdantflare_lyrics_aligner
COPY entrypoint.sh smoke-test.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/smoke-test.sh

ENV LYRICS_ALIGNER_MODEL_ROOT=/models/whisper \
    LYRICS_ALIGNER_MODEL=small \
    LYRICS_ALIGNER_LANGUAGE=zh \
    LYRICS_ALIGNER_TEMP_ROOT=/tmp/lyrics-aligner
VOLUME ["/models/whisper"]
EXPOSE 8000
HEALTHCHECK --start-period=20m --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
