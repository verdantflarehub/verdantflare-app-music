FROM docker.io/library/python:3.11.10-slim

ARG PYPI_INDEX_URL=https://pypi.org/simple
LABEL org.opencontainers.image.source="https://github.com/verdantflarehub/verdantflare-app-music"
LABEL org.opencontainers.image.title="VerdantFlare Music MCP Server"
LABEL org.opencontainers.image.version="0.1.0"
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" -r requirements.txt \
    && pip check
COPY verdantflare_music_mcp ./verdantflare_music_mcp
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1
CMD ["python3", "-m", "uvicorn", "verdantflare_music_mcp.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
