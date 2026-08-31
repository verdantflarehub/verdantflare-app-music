from __future__ import annotations

import contextlib
import hmac
import json
import os

from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from .artifacts import ArtifactError, ArtifactNotFound, ArtifactStore
from .executor import ExecutionError, VideoExecutor
from .tasks import TaskConflict, TaskNotFound, TaskStore

artifacts = ArtifactStore.from_environment()
tasks = TaskStore.from_environment()
executor = VideoExecutor(artifacts, tasks)
mcp = MCPServer("VerdantFlare Video")


def _result(value: dict[str, object]) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))], structuredContent=value)


@mcp.tool(name="artifact.import")
def artifact_import(project_id: str, source_url: str, filename: str, expected_sha256: str) -> types.CallToolResult:
    record = executor.import_asset(project_id=project_id, source_url=source_url, filename=filename, expected_sha256=expected_sha256)
    return _result({"status": "completed", "project_id": project_id, "artifact": record.model_dump(),
                    "download_path": artifacts.download_path(record.artifact_id)})


@mcp.tool(name="video.generate")
def video_generate(project_id: str, idempotency_key: str, model: str, prompt: str,
                   duration_seconds: int, aspect_ratio: str,
                   references: dict[str, list[dict[str, str]]]) -> types.CallToolResult:
    record = executor.generate(project_id=project_id, idempotency_key=idempotency_key, model=model,
                               prompt=prompt, duration_seconds=duration_seconds,
                               aspect_ratio=aspect_ratio, references=references)
    return _result({"video_task_id": record.video_task_id, "status": record.status, "created_at": record.created_at})


@mcp.tool(name="video.status")
def video_status(video_task_id: str) -> types.CallToolResult:
    record = executor.status(video_task_id)
    return _result({"video_task_id": record.video_task_id, "status": record.status,
                    "created_at": record.created_at, "updated_at": record.updated_at, "error": record.error})


@mcp.tool(name="video.result")
def video_result(video_task_id: str) -> types.CallToolResult:
    record = executor.result(video_task_id)
    artifact = artifacts.get(record.artifact_id, record.project_id)
    value = {"video_task_id": record.video_task_id, "artifact_id": artifact.artifact_id,
             "model": record.request["model"], "runtime_version": executor.runtime_version,
             "input_digest": record.input_digest, "media": record.media,
             "download_path": artifacts.download_path(artifact.artifact_id)}
    return _result(value)


def transport_security_from_environment() -> TransportSecuritySettings:
    hosts = [x.strip() for x in os.environ.get("VIDEO_MCP_ALLOWED_HOSTS", "").split(",") if x.strip()]
    origins = [x.strip() for x in os.environ.get("VIDEO_MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts or ["127.0.0.1:*", "localhost:*", "[::1]:*"],
                                     allowed_origins=origins or ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"])


async def health(request: Request) -> JSONResponse:
    artifacts.ensure_ready(); tasks.ensure_ready()
    return JSONResponse({"status": "ok", "contract_version": "v1"})


async def artifact_content(request: Request) -> Response:
    try:
        record = artifacts.get(request.path_params["artifact_id"])
        return FileResponse(artifacts.content_path(record), media_type=record.media_type, filename=record.filename)
    except (ValueError, ArtifactNotFound):
        return JSONResponse({"error": "artifact_not_found"}, status_code=404)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health" or request.url.path.startswith("/runtime-artifacts/"):
            return await call_next(request)
        token = os.environ.get("VIDEO_MCP_BEARER_TOKEN", "").strip()
        if token and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    artifacts.ensure_ready(); tasks.ensure_ready()
    async with mcp.session_manager.run():
        yield


app = Starlette(routes=[Route("/health", health),
                        Route("/artifacts/{artifact_id:str}/content", artifact_content),
                        Route("/runtime-artifacts/{artifact_id:str}/content", artifact_content),
                        Mount("/", app=mcp.streamable_http_app(json_response=True, stateless_http=True,
                                                               transport_security=transport_security_from_environment()))],
                lifespan=lifespan)
app.add_middleware(BearerAuthMiddleware)

