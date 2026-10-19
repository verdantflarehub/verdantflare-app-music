from __future__ import annotations

import contextlib
import hmac
import os
import threading
import time
from typing import Any

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from . import __version__
from .artifacts import ArtifactError, ArtifactNotFound, ArtifactRecord, ArtifactStore
from .dashboard import MusicDashboard
from .duet import DuetLine
from .executor import MusicExecutor, ServiceURLs
from .registry import EtcdRegistry
from .results import artifact_result, structured_result
from .tasks import TaskStore


store = ArtifactStore.from_environment()
task_store = TaskStore(store)
executor = MusicExecutor(store, ServiceURLs.from_environment())
mcp = MCPServer("VerdantFlare Music")


def _gpu_idle_loop() -> None:
    while True:
        time.sleep(30)
        try:
            executor.gpu.maybe_scale_down()
        except Exception:
            pass


if executor.gpu.enabled:
    threading.Thread(target=_gpu_idle_loop, name="music-gpu-idle", daemon=True).start()


def _emit_artifact_result(
    project_id: str,
    operation: str,
    records: list[ArtifactRecord],
    params: dict[str, Any] | None = None,
) -> types.CallToolResult:
    try:
        active_task_store = task_store if task_store.store is store else TaskStore(store)
        active_task_store.record_operation(
            project_id=project_id,
            operation=operation,
            records=records,
            params=params,
        )
    except Exception:
        pass
    return artifact_result(store, project_id, operation, records)


@mcp.tool(name="workflow.preflight")
def workflow_preflight(
    workflow: str,
    voice_source: str | None = None,
    voice_model_id: str | None = None,
    require_local_redraw: bool = False,
) -> types.CallToolResult:
    """Check technical readiness, exact voice-model availability, and redraw capability before creation."""
    return structured_result(
        executor.preflight(
            workflow=workflow,
            voice_source=voice_source,
            voice_model_id=voice_model_id,
            require_local_redraw=require_local_redraw,
        )
    )


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
    return _emit_artifact_result(
        project_id,
        "asset.import",
        records,
        {"source_url": source_url, "filename": filename},
    )


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
    return _emit_artifact_result(
        project_id,
        "music.generate",
        records,
        {
            "lyrics": lyrics,
            "instructions": instructions,
            "candidate_number": candidate_number,
            "seed": seed,
            "max_duration_seconds": max_duration_seconds,
        },
    )


@mcp.tool(name="duet.plan")
def duet_plan(
    project_id: str,
    lines: list[DuetLine],
    bpm: float,
    style: str,
    candidate_number: int,
) -> types.CallToolResult:
    """Save an immutable lyric-by-lyric female, male, and shared vocal timeline."""
    records = executor.duet_plan(
        project_id=project_id,
        lines=lines,
        bpm=bpm,
        style=style,
        candidate_number=candidate_number,
    )
    return _emit_artifact_result(
        project_id,
        "duet.plan",
        records,
        {"bpm": bpm, "style": style, "candidate_number": candidate_number},
    )


@mcp.tool(name="duet.instrumental")
def duet_instrumental(
    project_id: str,
    plan_asset_id: str,
    seed: int = 7,
    resume_task_asset_id: str | None = None,
) -> types.CallToolResult:
    """Generate an instrumental on the approved duet timeline with ACE-Step base."""
    records = executor.duet_instrumental(
        project_id=project_id,
        plan_asset_id=plan_asset_id,
        seed=seed,
        resume_task_asset_id=resume_task_asset_id,
    )
    return _emit_artifact_result(
        project_id,
        "duet.instrumental",
        records,
        {"plan_asset_id": plan_asset_id, "seed": seed},
    )


@mcp.tool(name="duet.vocal")
def duet_vocal(
    project_id: str,
    plan_asset_id: str,
    instrumental_asset_id: str,
    voice: str,
    seed: int = 7,
    resume_task_asset_id: str | None = None,
) -> types.CallToolResult:
    """Generate one independent female or male vocal layer over the duet instrumental."""
    records = executor.duet_vocal(
        project_id=project_id,
        plan_asset_id=plan_asset_id,
        instrumental_asset_id=instrumental_asset_id,
        voice=voice,
        seed=seed,
        resume_task_asset_id=resume_task_asset_id,
    )
    return _emit_artifact_result(
        project_id,
        "duet.vocal",
        records,
        {"plan_asset_id": plan_asset_id, "instrumental_asset_id": instrumental_asset_id, "voice": voice},
    )


@mcp.tool(name="duet.preview")
def duet_preview(
    project_id: str,
    plan_asset_id: str,
    instrumental_asset_id: str,
    female_asset_id: str,
    male_asset_id: str,
) -> types.CallToolResult:
    """Create a listening preview from the two distinct vocal tracks and instrumental."""
    records = executor.duet_preview(
        project_id=project_id,
        plan_asset_id=plan_asset_id,
        instrumental_asset_id=instrumental_asset_id,
        female_asset_id=female_asset_id,
        male_asset_id=male_asset_id,
    )
    return _emit_artifact_result(
        project_id,
        "duet.preview",
        records,
        {
            "plan_asset_id": plan_asset_id,
            "instrumental_asset_id": instrumental_asset_id,
            "female_asset_id": female_asset_id,
            "male_asset_id": male_asset_id,
        },
    )


@mcp.tool(name="music.redraw")
def music_redraw(
    project_id: str,
    audio_asset_id: str,
    start_seconds: float,
    end_seconds: float,
    instructions: str,
    revision_number: int,
    lyrics: str = "",
    seed: int = 7,
    crossfade_seconds: float = 1.0,
    preservation_mode: str = "balanced",
    edit_strength: float = 0.5,
) -> types.CallToolResult:
    """Replace one bounded region through a configured conditioned-audio editor and return a full-song WAV."""
    records = executor.redraw(
        project_id=project_id,
        audio_asset_id=audio_asset_id,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        instructions=instructions,
        lyrics=lyrics,
        revision_number=revision_number,
        seed=seed,
        crossfade_seconds=crossfade_seconds,
        preservation_mode=preservation_mode,
        edit_strength=edit_strength,
    )
    return _emit_artifact_result(
        project_id,
        "music.redraw",
        records,
        {
            "audio_asset_id": audio_asset_id,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "instructions": instructions,
            "revision_number": revision_number,
        },
    )


@mcp.tool(name="stems.separate")
def stems_separate(project_id: str, audio_asset_id: str) -> types.CallToolResult:
    """Separate a persisted project audio artifact into instrumental and dry vocal artifacts."""
    records = executor.separate_stems(project_id=project_id, audio_asset_id=audio_asset_id)
    return _emit_artifact_result(
        project_id,
        "stems.separate",
        records,
        {"audio_asset_id": audio_asset_id},
    )


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
    return _emit_artifact_result(
        project_id,
        "voice.train",
        records,
        {"audio_asset_id": audio_asset_id, "model_id": model_id, "epochs": epochs},
    )


@mcp.tool(name="voice.prepare")
def voice_prepare(project_id: str, audio_asset_ids: list[str]) -> types.CallToolResult:
    """Prepare 1-20 authorized recordings as deterministic RVC training material for human review."""
    records = executor.prepare_voice(project_id=project_id, audio_asset_ids=audio_asset_ids)
    return _emit_artifact_result(
        project_id,
        "voice.prepare",
        records,
        {"audio_asset_ids": audio_asset_ids},
    )


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
    return _emit_artifact_result(
        project_id,
        "voice.convert",
        records,
        {
            "audio_asset_id": audio_asset_id,
            "model_id": model_id,
            "pitch_shift": pitch_shift,
            "f0_method": f0_method,
            "index_rate": index_rate,
        },
    )


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
    return _emit_artifact_result(
        project_id,
        "lyrics.align",
        records,
        {"vocal_asset_id": vocal_asset_id, "lyrics": lyrics, "language": language},
    )


@mcp.tool(name="mix.master")
def mix_master(
    project_id: str,
    instrumental_asset_id: str,
    vocal_asset_id: str,
    lyrics_lrc: str,
    bpm: float,
    backing_vocal_asset_id: str | None = None,
    backing_gain_db: float = -6.0,
    additional_vocal_asset_ids: list[str] | None = None,
    additional_vocal_gains_db: list[float] | None = None,
    vocal_mode: str = "solo",
) -> types.CallToolResult:
    """Mix and master approved project artifacts with aligned LRC text into final delivery artifacts."""
    records = executor.master(
        project_id=project_id,
        instrumental_asset_id=instrumental_asset_id,
        vocal_asset_id=vocal_asset_id,
        lyrics_lrc=lyrics_lrc,
        bpm=bpm,
        backing_vocal_asset_id=backing_vocal_asset_id,
        backing_gain_db=backing_gain_db,
        additional_vocal_asset_ids=additional_vocal_asset_ids,
        additional_vocal_gains_db=additional_vocal_gains_db,
        vocal_mode=vocal_mode,
    )
    return _emit_artifact_result(
        project_id,
        "mix.master",
        records,
        {
            "instrumental_asset_id": instrumental_asset_id,
            "vocal_asset_id": vocal_asset_id,
            "lyrics_lrc": lyrics_lrc,
            "bpm": bpm,
            "vocal_mode": vocal_mode,
        },
    )


def _extract_tools_schema() -> list[dict[str, Any]]:
    raw_tools = getattr(getattr(mcp, "_tool_manager", None), "list_tools", lambda: [])()
    items: list[dict[str, Any]] = []
    for t in raw_tools:
        name = getattr(t, "name", "")
        desc = getattr(t, "description", "") or ""
        params = getattr(t, "parameters", None) or getattr(t, "input_schema", None) or {}
        if name:
            items.append({"name": name, "description": desc, "inputSchema": params})
    return items


from .production import register as register_production

production_jobs = register_production(mcp, executor, _emit_artifact_result)

TOOLS_SCHEMA = _extract_tools_schema()
registry = EtcdRegistry(domain="music", tools=TOOLS_SCHEMA, version=__version__)
dashboard = MusicDashboard(
    store=store,
    tasks=task_store,
    executor=executor,
    registry=registry,
    tools_schema=TOOLS_SCHEMA,
)


async def health(request: Request) -> JSONResponse:
    try:
        store.ensure_ready()
    except ArtifactError:
        return JSONResponse({"status": "unavailable", "contract_version": "v1"}, status_code=503)
    return JSONResponse(
        {
            "status": "ok",
            "contract_version": "v1",
            "version": __version__,
            "execution": "direct",
        }
    )


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
    PUBLIC_INTERNAL_PREFIXES = ("/internal/dashboard", "/dashboard", "/api/")

    def __init__(self, app: Starlette, token: str | None) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path == "/health" or path.startswith(self.PUBLIC_INTERNAL_PREFIXES) or self.token is None:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    store.ensure_ready()
    task_store.sync_from_artifacts()
    await registry.start()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        await registry.stop()


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/artifacts/{artifact_id:str}/content", artifact_content),
        *dashboard.routes(),
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
