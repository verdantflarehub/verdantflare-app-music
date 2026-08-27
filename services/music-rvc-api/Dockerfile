ARG BASE_IMAGE=docker.io/pytorch/pytorch:2.4.1-cuda12.4-cudnn9-devel
FROM ${BASE_IMAGE}

USER root

ARG DEBIAN_FRONTEND=noninteractive
ARG RVC_UPSTREAM_VERSION=2.2.231006
ARG RVC_COMMIT=9f2f0559e6932c10c48642d404e7d2e771d9db43
ARG SERVICE_VERSION=0.1.4
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG PYTORCH_CUDA_INDEX_URL=https://download.pytorch.org/whl/cu124

LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare Music RVC Runtime"
LABEL org.opencontainers.image.version="${SERVICE_VERSION}"
LABEL org.opencontainers.image.licenses="MIT"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        aria2 \
        build-essential \
        ffmpeg \
        git \
        libsndfile1-dev \
        libsox-fmt-all \
        libsox-fmt-mp3 \
        sox; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt /tmp/rvc-requirements.txt
COPY entrypoint.sh /usr/local/bin/music-rvc-entrypoint
COPY LICENSE.upstream /usr/share/licenses/music-rvc-api/LICENSE.upstream
COPY verdantflare_rvc /workspace/rvc-api/verdantflare_rvc
RUN chmod 0755 /usr/local/bin/music-rvc-entrypoint

RUN pip install --no-cache-dir --no-deps \
        --index-url "${PYTORCH_CUDA_INDEX_URL}" \
        torchaudio==2.4.1 \
        torchvision==0.19.1

RUN set -eux; \
    git clone --branch "${RVC_UPSTREAM_VERSION}" --depth 1 \
        https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git rvc; \
    test "$(git -C rvc rev-parse HEAD)" = "${RVC_COMMIT}"; \
    sed -i 's/av.open(i, "rb")/av.open(i, "r")/' rvc/infer/lib/audio.py; \
    sed -i 's/av.open(o, "wb", format=format)/av.open(o, "w", format=format)/' rvc/infer/lib/audio.py; \
    grep -F 'av.open(i, "r")' rvc/infer/lib/audio.py; \
    grep -F 'av.open(o, "w", format=format)' rvc/infer/lib/audio.py; \
    rm -rf rvc/.git

RUN set -eux; \
    pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" "pip<24.1"; \
    pip install --no-cache-dir \
        --index-url "${PYPI_INDEX_URL}" \
        --extra-index-url "${PYTORCH_CUDA_INDEX_URL}" \
        -r /tmp/rvc-requirements.txt

# fairseq 0.12.2 and its Hydra dependency use mutable dataclass defaults that
# Python 3.11 rejects. Keep their declared dependency versions and patch only
# those defaults; fairseq's global Hydra registration is not used by RVC.
RUN python -c "import re, glob; [(lambda c, f=f: open(f, 'w').write(('from dataclasses import field\n' if 'from dataclasses import field' not in c else '') + re.sub(r'([a-zA-Z0-9_]+):\s*([a-zA-Z0-9_]+)\s*=\s*\2\(\)', r'\1: \2 = field(default_factory=\2)', re.sub(r'([a-zA-Z0-9_]+):\s*([a-zA-Z0-9_]+)\s*=\s*field\(default=\2\(\)\)', r'\1: \2 = field(default_factory=\2)', c))))(open(f).read()) for p in ('/opt/conda/lib/python3.11/site-packages/fairseq/**/*.py', '/opt/conda/lib/python3.11/site-packages/hydra/**/*.py') for f in glob.glob(p, recursive=True) if re.search(r'([a-zA-Z0-9_]+):\s*([a-zA-Z0-9_]+)\s*=\s*(?:field\(default=)?\2\(\)', open(f).read())]"; \
    sed -i 's/hydra_init()/# hydra_init()/g' /opt/conda/lib/python3.11/site-packages/fairseq/__init__.py

RUN set -eux; \
    pip check; \
    PYTHONPATH=/workspace/rvc-api python -c "import torch; assert torch.__version__.split('+')[0] == '2.4.1', torch.__version__; import torchaudio; assert torchaudio.__version__.split('+')[0] == '2.4.1', torchaudio.__version__; import fairseq; import fastapi; import faiss; import librosa; import multipart; import uvicorn; import verdantflare_rvc.api; print('RVC runtime imports OK')"

ENV PYTHONPATH=/workspace/rvc-api \
    RVC_RUNTIME_ROOT=/models/rvc/runtime \
    RVC_VOICE_ROOT=/models/rvc/voices \
    RVC_TEMP_ROOT=/tmp/rvc

VOLUME ["/models/rvc"]

EXPOSE 8000

WORKDIR /workspace/rvc

HEALTHCHECK --start-period=10m --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/local/bin/music-rvc-entrypoint"]
