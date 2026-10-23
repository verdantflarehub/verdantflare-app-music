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
from .review_rules import MUSIC_REVIEW_RULES
from .artifacts import ArtifactError, ArtifactNotFound, ArtifactRecord, ArtifactStore
from .content_rating import ContentRatingError, ContentRatingStore, include_nsfw_from_headers
from .dashboard import MusicDashboard
from .duet import DuetLine
from .executor import MusicExecutor, ServiceURLs
from .registry import EtcdRegistry
from .results import artifact_result, structured_result
from .tasks import TaskNotFound, TaskStore


store = ArtifactStore.from_environment()
artifacts = store
ratings = ContentRatingStore(store.root / "tasks.db", domain="music")
task_store = TaskStore(store, ratings=ratings)
tasks = task_store
_DEFAULT_STORE = store
_DEFAULT_TASKS = task_store
_DEFAULT_RATINGS = ratings
executor = MusicExecutor(store, ServiceURLs.from_environment())
mcp = MCPServer("VerdantFlare Music")


def _active_store() -> ArtifactStore:
    if artifacts is not _DEFAULT_STORE and store is _DEFAULT_STORE:
        return artifacts
    return store


def _active_ratings() -> ContentRatingStore:
    global ratings
    active_s = _active_store()
    active_t = task_store if tasks is _DEFAULT_TASKS else tasks
    expected_db = getattr(active_t, "db_path", active_s.root / "tasks.db")
    if ratings is not _DEFAULT_RATINGS:
        return ratings
    if ratings.db_path != expected_db:
        ratings = ContentRatingStore(expected_db, domain="music")
    return ratings


def _active_tasks() -> TaskStore:
    active_s = _active_store()
    active_r = _active_ratings()
    active_t = tasks if tasks is not _DEFAULT_TASKS else task_store
    if active_t.store is not active_s:
        active_t.store = active_s
        active_t.tasks_file = active_s.root / "tasks.json"
        active_t.db_path = active_s.root / "tasks.db"
    active_t.ratings = active_r
    return active_t


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
    active_s = _active_store()
    try:
        active_task_store = _active_tasks()
        active_task_store.record_operation(
            project_id=project_id,
            operation=operation,
            records=records,
            params=params,
        )
    except Exception:
        pass
    return artifact_result(active_s, project_id, operation, records)


@mcp.tool(name="workflow.preflight", description='Check technical readiness, voice-model availability and redraw capability. Readiness is not musical approval.' + "\n\n" + MUSIC_REVIEW_RULES)
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


@mcp.tool(name="music.generate", description='Generate an original song from lyrics and instructions; this is not performance-preserving rearrangement.' + "\n\n" + MUSIC_REVIEW_RULES)
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


@mcp.tool(name="music.redraw", description='ACE-Step edits an existing bounded region only; never use it to compose new verses or extend a song. Use music.conditioning_check first (caption 192 tokens, lyrics 1800), and set vocal_language explicitly (ja for Japanese). Crossfade_seconds governs the final local PCM splice; backend preservation_mode resolves its own latent blending. In-region harmony and rhythm are not guaranteed preserved.' + "\n\n" + MUSIC_REVIEW_RULES)
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
    vocal_language: str = "unknown",
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
        vocal_language=vocal_language,
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


@mcp.tool(name="mix.master", description='Mix and master approved project artifacts with aligned LRC text.' + "\n\n" + MUSIC_REVIEW_RULES)
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


def _resolve_rating_target(version_id: str, project_id: str | None = None) -> tuple[str, str | None]:
    """Resolve (project_id, task_id) for an artifact_id or task_id, or raise TaskNotFound."""
    vid = (version_id or "").strip()
    if not vid:
        raise TaskNotFound("NOT_FOUND: version_id is required")
    active_t = _active_tasks()
    active_s = _active_store()
    matched_task = active_t.find_by_artifact_or_task(vid)
    if matched_task is not None:
        if project_id and matched_task.get("project_id") != project_id:
            raise TaskNotFound(f"NOT_FOUND: {vid}")
        return str(matched_task["project_id"]), str(matched_task["task_id"])
    try:
        art = active_s.get(vid, project_id)
        matched_after = active_t.find_by_artifact_or_task(vid)
        return art.project_id, (str(matched_after["task_id"]) if matched_after else None)
    except (ValueError, ArtifactNotFound) as exc:
        raise TaskNotFound(f"NOT_FOUND: {vid}") from exc


@mcp.tool(name="music.rate_content")
def music_rate_content(
    version_id: str,
    rating: str,
    reason: str | None = None,
    reasons: list[str] | None = None,
    rubric_version: str = "rubric v1.0",
    evaluator_model: str | None = None,
    project_id: str | None = None,
) -> types.CallToolResult:
    """Record content rating (general or nsfw) for a music artifact or task version, including lyrics evaluation rationale. Add-only; cannot clear an existing nsfw rating."""
    resolved_project_id, resolved_task_id = _resolve_rating_target(version_id, project_id)
    ratings_store = _active_ratings()
    res = ratings_store.rate_content(
        version_id=version_id,
        project_id=resolved_project_id,
        rating=rating,
        reasons=reasons,
        reason=reason,
        rubric_version=rubric_version,
        evaluator_model=evaluator_model,
        rated_by="skill:k09",
        task_id=resolved_task_id,
    )
    if resolved_task_id:
        matched_task = _active_tasks().find_by_artifact_or_task(version_id)
        if matched_task:
            arts = matched_task.get("artifacts", [])
            if version_id == resolved_task_id:
                for art in arts:
                    aid = art.get("artifact_id")
                    if aid and aid != version_id:
                        ratings_store.rate_content(
                            version_id=aid,
                            project_id=resolved_project_id,
                            rating=rating,
                            reasons=reasons,
                            reason=reason,
                            rubric_version=rubric_version,
                            evaluator_model=evaluator_model,
                            rated_by="skill:k09",
                            task_id=resolved_task_id,
                        )
            elif len(arts) == 1 and arts[0].get("artifact_id") == version_id:
                ratings_store.rate_content(
                    version_id=resolved_task_id,
                    project_id=resolved_project_id,
                    rating=rating,
                    reasons=reasons,
                    reason=reason,
                    rubric_version=rubric_version,
                    evaluator_model=evaluator_model,
                    rated_by="skill:k09",
                    task_id=resolved_task_id,
                )
    return structured_result(res)


@mcp.tool(name="music.status")
def music_status(task_id: str) -> types.CallToolResult:
    """Query status and metadata of a music task (NSFW tasks return NOT_FOUND)."""
    task = _active_tasks().get_task(task_id, include_nsfw=False)
    arts = task.get("artifacts", [])
    return structured_result(
        {
            "task_id": task["task_id"],
            "project_id": task.get("project_id"),
            "operation": task.get("operation"),
            "status": task.get("status", "completed"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "duration_seconds": task.get("duration_seconds"),
            "artifact_id": arts[0]["artifact_id"] if arts else None,
            "artifacts": arts,
        }
    )


@mcp.tool(name="music.result")
def music_result(task_id: str) -> types.CallToolResult:
    """Retrieve generated music artifacts, LRC lyrics, and download paths for a completed task (NSFW items are excluded)."""
    task = _active_tasks().get_task(task_id, include_nsfw=False)
    arts = task.get("artifacts", [])
    return structured_result(
        {
            "task_id": task["task_id"],
            "project_id": task.get("project_id"),
            "operation": task.get("operation"),
            "status": task.get("status", "completed"),
            "duration_seconds": task.get("duration_seconds"),
            "lyrics_lrc": task.get("lyrics_lrc"),
            "artifact_id": arts[0]["artifact_id"] if arts else None,
            "artifacts": arts,
        }
    )


@mcp.tool(name="music.list")
def music_list(
    project_id: str | None = None,
    operation: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> types.CallToolResult:
    """List music tasks and artifacts with pagination (NSFW items are unconditionally excluded)."""
    records, total = _active_tasks().query_tasks(
        project_id=project_id,
        operation=operation,
        status=status,
        limit=limit,
        offset=offset,
        include_nsfw=False,
    )
    return structured_result(
        {
            "tasks": records,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


def music_capabilities() -> types.CallToolResult:
    """Return Music MCP capabilities including content_rating support."""
    return structured_result(
        {
            "service_version": __version__,
            "content_rating": {
                "supported": True,
                "tool": "music.rate_content",
                "rubric_version": "rubric v1.0",
                "add_only": True,
            },
        }
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
MUSIC_TOOLS_SCHEMA = TOOLS_SCHEMA
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
        _active_store().ensure_ready()
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
        artifact_id = request.path_params["artifact_id"]
        include_nsfw = include_nsfw_from_headers(request.headers)
        if not include_nsfw and _active_tasks().is_artifact_nsfw(artifact_id):
            return JSONResponse({"error": "artifact_not_found"}, status_code=404)
        record = _active_store().get(artifact_id)
        path = _active_store().content_path(record)
    except (ValueError, ArtifactNotFound):
        return JSONResponse({"error": "artifact_not_found"}, status_code=404)
    except ArtifactError:
        return JSONResponse({"error": "artifact_unavailable"}, status_code=503)
    return FileResponse(path, media_type=record.media_type, filename=record.filename)


async def internal_clear_rating(request: Request) -> JSONResponse:
    version_id = (request.path_params.get("version_id") or "").strip()
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    project_id = body.get("project_id") or request.headers.get("X-Project-Id") or None
    note = body.get("note") or body.get("reason")
    actor_id = (
        request.headers.get("X-User-Id", "").strip()
        or body.get("actor_user_id")
        or body.get("actor_id")
        or "human"
    )
    ratings_store = _active_ratings()
    try:
        resolved_project_id, resolved_task_id = _resolve_rating_target(version_id, project_id)
        rec = ratings_store.clear_rating(
            version_id=version_id,
            project_id=resolved_project_id,
            actor_user_id=actor_id,
            note=note,
            task_id=resolved_task_id,
        )
        matched_task = _active_tasks().find_by_artifact_or_task(version_id)
        if matched_task:
            arts = matched_task.get("artifacts", [])
            if version_id == matched_task.get("task_id"):
                for art in arts:
                    aid = art.get("artifact_id")
                    if aid and aid != version_id:
                        ratings_store.clear_rating(
                            version_id=aid,
                            project_id=resolved_project_id,
                            actor_user_id=actor_id,
                            note=note,
                            task_id=resolved_task_id,
                        )
            elif matched_task.get("task_id") and (
                len(arts) == 1 or ratings_store.get(matched_task["task_id"]).get("rating") != "unrated"
            ):
                ratings_store.clear_rating(
                    version_id=matched_task["task_id"],
                    project_id=resolved_project_id,
                    actor_user_id=actor_id,
                    note=note,
                    task_id=resolved_task_id,
                )
        return JSONResponse(rec)
    except TaskNotFound:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except ContentRatingError as exc:
        return JSONResponse({"error": exc.error_code, "message": exc.message}, status_code=exc.status_code)


async def internal_mark_rating(request: Request) -> JSONResponse:
    version_id = (request.path_params.get("version_id") or "").strip()
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    project_id = body.get("project_id") or request.headers.get("X-Project-Id") or None
    reason = body.get("reason") or body.get("note")
    actor_id = (
        request.headers.get("X-User-Id", "").strip()
        or body.get("actor_user_id")
        or body.get("actor_id")
        or "human"
    )
    ratings_store = _active_ratings()
    try:
        resolved_project_id, resolved_task_id = _resolve_rating_target(version_id, project_id)
        rec = ratings_store.mark_rating(
            version_id=version_id,
            project_id=resolved_project_id,
            actor_user_id=actor_id,
            reason=reason,
            task_id=resolved_task_id,
        )
        matched_task = _active_tasks().find_by_artifact_or_task(version_id)
        if matched_task:
            arts = matched_task.get("artifacts", [])
            if version_id == matched_task.get("task_id"):
                for art in arts:
                    aid = art.get("artifact_id")
                    if aid and aid != version_id:
                        ratings_store.mark_rating(
                            version_id=aid,
                            project_id=resolved_project_id,
                            actor_user_id=actor_id,
                            reason=reason,
                            task_id=resolved_task_id,
                        )
            elif len(arts) == 1 and matched_task.get("task_id"):
                ratings_store.mark_rating(
                    version_id=matched_task["task_id"],
                    project_id=resolved_project_id,
                    actor_user_id=actor_id,
                    reason=reason,
                    task_id=resolved_task_id,
                )
        return JSONResponse(rec)
    except TaskNotFound:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except ContentRatingError as exc:
        return JSONResponse({"error": exc.error_code, "message": exc.message}, status_code=exc.status_code)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    PUBLIC_INTERNAL_PREFIXES = (
        "/internal/dashboard",
        "/music/internal/dashboard",
        "/dashboard",
        "/music/dashboard",
        "/api/",
        "/music/api/",
    )

    def __init__(self, app: Starlette, token: str | None = None) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        effective_token = (
            self.token
            if self.token is not None
            else (os.environ.get("MUSIC_MCP_BEARER_TOKEN", "").strip() or None)
        )
        if path in ("/health", "/music/health") or path.startswith(self.PUBLIC_INTERNAL_PREFIXES) or effective_token is None:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {effective_token}"
        if not hmac.compare_digest(authorization, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    _active_store().ensure_ready()
    _active_ratings().ensure_ready()
    _active_tasks().sync_from_artifacts()
    await registry.start()
    try:
        if getattr(mcp.session_manager, "_has_started", False):
            mcp.session_manager._has_started = False
        async with mcp.session_manager.run():
            yield
    finally:
        await registry.stop()


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/music/health", health),
        Route("/artifacts/{artifact_id:str}/content", artifact_content),
        Route("/music/artifacts/{artifact_id:str}/content", artifact_content),
        Route("/internal/content-ratings/{version_id}/clear", internal_clear_rating, methods=["POST"]),
        Route("/music/internal/content-ratings/{version_id}/clear", internal_clear_rating, methods=["POST"]),
        Route("/internal/content-ratings/{version_id}/mark", internal_mark_rating, methods=["POST"]),
        Route("/music/internal/content-ratings/{version_id}/mark", internal_mark_rating, methods=["POST"]),
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
app.state.store = store
app.state.artifacts = store
app.state.tasks = task_store
app.state.task_store = task_store
app.state.ratings = ratings
app.add_middleware(
    BearerAuthMiddleware,
    token=os.environ.get("MUSIC_MCP_BEARER_TOKEN", "").strip() or None,
)
