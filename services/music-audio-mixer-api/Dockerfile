FROM docker.io/library/python:3.11.10-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG PYPI_INDEX_URL=https://pypi.org/simple
LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare Music Audio Mixer API"
LABEL org.opencontainers.image.version="0.1.0"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" -r requirements.txt \
    && pip check
COPY NOTICE.upstream.md /usr/share/licenses/music-audio-mixer-api/NOTICE.upstream.md
COPY verdantflare_mixer ./verdantflare_mixer
COPY entrypoint.sh smoke-test.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/smoke-test.sh
ENV MIXER_TEMP_ROOT=/tmp/music-mixer
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
