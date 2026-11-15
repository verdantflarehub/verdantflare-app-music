# syntax=docker/dockerfile:1.7
FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ACESTEP_REV=ca1e85fe9430179831e6bc6be790c332190a3866

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare ACE-Step 1.5"
LABEL org.opencontainers.image.version="0.1.2"
LABEL org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends software-properties-common git curl ca-certificates ffmpeg libsndfile1 libsndfile1-dev build-essential libffi-dev libssl-dev \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3.11 python3.11-dev python3.11-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /uvx /usr/local/bin/

WORKDIR /app
RUN git clone --filter=blob:none https://github.com/ace-step/ACE-Step-1.5.git /app \
    && git checkout "${ACESTEP_REV}" \
    && rm -rf .git \
    && uv sync --frozen --no-dev --python python3.11
COPY guard/ /opt/acestep-guard/guard/
COPY entrypoint.sh /usr/local/bin/music-ace-step-entrypoint
COPY NOTICE.upstream.md /usr/share/licenses/music-ace-step/NOTICE.upstream.md
RUN chmod 0755 /usr/local/bin/music-ace-step-entrypoint && /app/.venv/bin/python -m compileall -q /opt/acestep-guard

ENV ACESTEP_API_HOST=0.0.0.0 \
    ACESTEP_API_PORT=8001 \
    ACESTEP_CONFIG_PATH=acestep-v15-base \
    ACESTEP_NO_INIT=false \
    ACESTEP_INIT_LLM=false \
    ACESTEP_DOWNLOAD_SOURCE=modelscope \
    TOKENIZERS_PARALLELISM=false

EXPOSE 8001
ENTRYPOINT ["/usr/local/bin/music-ace-step-entrypoint"]
