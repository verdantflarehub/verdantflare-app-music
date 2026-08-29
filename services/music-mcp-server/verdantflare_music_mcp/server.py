from __future__ import annotations

import contextlib
import hmac
import os

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from .artifacts import ArtifactError, ArtifactNotFound, ArtifactStore
from .executor import MusicExecutor, ServiceURLs
from .results import artifact_result


store = ArtifactStore.from_environment()
executor = MusicExecutor(store, ServiceURLs.from_environment())
mcp = MCPServer("VerdantFlare Music")


def _csv_environment(name: str, defaults: list[str]) -> list[str]:
    configured = [value.strip() for value in os.environ.get(name, "").split(",") if value.strip()]
    return configured or defaults


def transport_security_from_environment() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv_environment(
            "MUSIC_MCP_ALLOWED_HOSTS",
            ["127.0.0.1:*", "localhost:*", "[::1]:*"],
        ),
        allowed_origins=_csv_environment(
            "MUSIC_MCP_ALLOWED_ORIGINS",
            ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )


@mcp.tool(name="asset.import")
def asset_import(
    project_id: str,
    source_url: str,
    filename: str,
    expected_sha256: str,
) -> types.CallToolResult:
    """Import an approved S3 audio object into project-scoped Artifact storage after integrity verification."""
    records = executor.import_asset(
        project_id=project_id,
        source_url=source_url,
        filename=filename,
        expected_sha256=expected_sha256,
    )
    return artifact_result(store, project_id, "asset.import", records)


@mcp.tool(name="music.generate")
def music_generate(
    project_id: str,
    lyrics: str,
    instructions: str,
    candidate_number: int,
    seed: int = 7,
    max_duration_seconds: float = 200.0,
) -> types.CallToolResult:
    """Generate one approved Music3 candidate and preserve its natural ending within the duration cap."""
    records = executor.generate(
        project_id=project_id,
        lyrics=lyrics,
        instructions=instructions,
        candidate_number=candidate_number,
        seed=seed,
        max_duration_seconds=max_duration_seconds,
    )
    return artifact_result(store, project_id, "music.generate", records)


@mcp.tool(name="stems.separate")
def stems_separate(project_id: str, audio_asset_id: str) -> types.CallToolResult:
    """Separate a persisted project audio artifact into instrumental and dry vocal artifacts."""
    records = executor.separate_stems(project_id=project_id, audio_asset_id=audio_asset_id)
    return artifact_result(store, project_id, "stems.separate", records)


@mcp.tool(name="voice.train")
def voice_train(
    project_id: str,
    audio_asset_id: str,
    model_id: str,
    epochs: int = 200,
    batch_size: int = 4,
) -> types.CallToolResult:
    """Train a new RVC model from an authorized recording artifact and persist its package outputs."""
    records = executor.train_voice(
        project_id=project_id,
        audio_asset_id=audio_asset_id,
        model_id=model_id,
        epochs=epochs,
        batch_size=batch_size,
    )
    return artifact_result(store, project_id, "voice.train", records)


@mcp.tool(name="voice.convert")
def voice_convert(
    project_id: str,
    audio_asset_id: str,
    model_id: str,
    pitch_shift: int = 0,
    f0_method: str = "rmvpe",
    index_rate: float = 0.66,
    filter_radius: int = 3,
    rms_mix_rate: float = 1.0,
    protect: float = 0.33,
) -> types.CallToolResult:
    """Convert a dry vocal artifact with an installed approved RVC model and explicit quality controls."""
    records = executor.convert_voice(
        project_id=project_id,
        audio_asset_id=audio_asset_id,
        model_id=model_id,
        pitch_shift=pitch_shift,
        f0_method=f0_method,
        index_rate=index_rate,
        filter_radius=filter_radius,
        rms_mix_rate=rms_mix_rate,
        protect=protect,
    )
    return artifact_result(store, project_id, "voice.convert", records)


@mcp.tool(name="lyrics.align")
def lyrics_align(
    project_id: str,
    vocal_asset_id: str,
    lyrics: str,
    language: str = "zh",
) -> types.CallToolResult:
    """Force-align approved lyric lines to a persisted project vocal artifact."""
    records = executor.align_lyrics(
        project_id=project_id,
        vocal_asset_id=vocal_asset_id,
        lyrics=lyrics,
        language=language,
    )
    return artifact_result(store, project_id, "lyrics.align", records)


@mcp.tool(name="mix.master")
def mix_master(
    project_id: str,
    instrumental_asset_id: str,
    vocal_asset_id: str,
    lyrics_lrc: str,
    bpm: float,
) -> types.CallToolResult:
    """Mix and master approved project artifacts with aligned LRC text into final delivery artifacts."""
    records = executor.master(
        project_id=project_id,
        instrumental_asset_id=instrumental_asset_id,
        vocal_asset_id=vocal_asset_id,
        lyrics_lrc=lyrics_lrc,
        bpm=bpm,
    )
    return artifact_result(store, project_id, "mix.master", records)


async def health(request: Request) -> JSONResponse:
    try:
        store.ensure_ready()
    except ArtifactError:
        return JSONResponse({"status": "unavailable", "contract_version": "v1"}, status_code=503)
    return JSONResponse({"status": "ok", "contract_version": "v1", "execution": "direct"})


async def artifact_content(request: Request) -> Response:
    try:
        record = store.get(request.path_params["artifact_id"])
        path = store.content_path(record)
    except (ValueError, ArtifactNotFound):
        return JSONResponse({"error": "artifact_not_found"}, status_code=404)
    except ArtifactError:
        return JSONResponse({"error": "artifact_unavailable"}, status_code=503)
    return FileResponse(path, media_type=record.media_type, filename=record.filename)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Starlette, token: str | None) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/health" or self.token is None:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    store.ensure_ready()
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/artifacts/{artifact_id:str}/content", artifact_content),
        Mount(
            "/",
            app=mcp.streamable_http_app(
                json_response=True,
                stateless_http=True,
                transport_security=transport_security_from_environment(),
            ),
        ),
    ],
    lifespan=lifespan,
)
app.add_middleware(
    BearerAuthMiddleware,
    token=os.environ.get("MUSIC_MCP_BEARER_TOKEN", "").strip() or None,
)
