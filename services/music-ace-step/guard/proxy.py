"""ASGI receiver-guard proxy in front of acestep.api_server (127.0.0.1:18001) for ACE-Step 1.5."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import httpx

from .gpu_guard import (
    FencePersistenceError,
    LeaseFenceGuard,
    StaleFenceError,
    read_runtime_identity,
    verify_internal_bearer,
)


def create_acestep_guard_app(
    *,
    upstream_url: str | None = None,
    state_path: str | Path | None = None,
    init_pid: int = 0,
    upstream_leader_pid: int = 0,
    upstream_pid_file: str | Path | None = None,
    proc_root: str | Path = "/proc",
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved_upstream = (upstream_url or os.environ.get("ACESTEP_UPSTREAM_URL", "http://127.0.0.1:18001")).rstrip("/")
    resolved_state = state_path or os.environ.get("VF_GPU_GUARD_STATE_PATH", "/tmp/acestep-guard/gpu_guard_state.json")
    fence_guard = LeaseFenceGuard(
        state_path=resolved_state,
        init_pid=init_pid,
        upstream_leader_pid=upstream_leader_pid,
        upstream_pid_file=upstream_pid_file,
        proc_root=proc_root,
    )

    async def _reconcile_running_tasks() -> None:
        running_tids = fence_guard.list_running_upstream_task_ids()
        if not running_tids:
            return
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = os.environ.get("ACESTEP_API_KEY", "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(transport=transport, timeout=5.0, trust_env=False) as client:
                resp = await client.post(
                    f"{resolved_upstream}/query_result",
                    json={"task_id_list": running_tids},
                    headers=headers,
                )
            if 200 <= resp.status_code < 300 and resp.content:
                doc = resp.json()
                items = doc.get("data") if isinstance(doc, dict) else None
                if isinstance(items, list):
                    settled = [
                        str(it.get("task_id") or "")
                        for it in items
                        if isinstance(it, dict) and it.get("status") in (1, 2) and it.get("task_id")
                    ]
                    if settled:
                        fence_guard.complete_upstream_tasks(settled)
        except (httpx.HTTPError, ValueError):
            return

    app = FastAPI(title="VerdantFlare ACE-Step Guard Proxy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fence_guard = fence_guard

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        ident = read_runtime_identity()
        _, upstream_ident, upstream_configured = fence_guard.inspect_topology(supervisor_pid=int(ident["supervisor_pid"]))
        active = fence_guard.active_count()
        if upstream_configured and upstream_ident is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "active_tasks": active,
                    "residency_state": "S3",
                    "runtime_identity": ident,
                },
            )
        fwd_hdrs = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        try:
            async with httpx.AsyncClient(transport=transport, timeout=5.0, trust_env=False) as client:
                up = await client.get(f"{resolved_upstream}/health", headers=fwd_hdrs)
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "active_tasks": active, "residency_state": "S3", "runtime_identity": ident},
            )
        base: dict[str, object] = {}
        if up.content:
            try:
                parsed = up.json()
                if isinstance(parsed, dict):
                    base.update(parsed)
            except ValueError:
                pass
        ok = 200 <= up.status_code < 300 and base.get("status") not in ("unavailable", "error", "failed")
        base["status"] = "ok" if ok else "unavailable"
        base["active_tasks"] = active
        base["residency_state"] = "S1" if active > 0 else ("S2" if ok else "S3")
        base["runtime_identity"] = ident
        return JSONResponse(status_code=200 if ok else 503, content=base)

    @app.post("/internal/v1/gpu-guard/reclaim")
    async def reclaim_guard(request: Request) -> JSONResponse:
        if not verify_internal_bearer(request.headers.get("authorization", "")):
            raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="INVALID_JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="INVALID_JSON")
        lease_id = str(body.get("lease_id") or "").strip()
        try:
            fencing_token = int(body.get("fencing_token") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="INVALID_FENCING_TOKEN") from exc
        if not lease_id and fencing_token <= 0:
            raise HTTPException(status_code=400, detail="INVALID_REQUEST")
        try:
            await _reconcile_running_tasks()
            proof = fence_guard.reclaim_and_seal(lease_id=lease_id, fencing_token=fencing_token, settled=True)
        except FencePersistenceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        active = int(proof["active_tasks"]) if isinstance(proof, dict) else fence_guard.active_count()
        settled = bool(isinstance(proof, dict) and proof.get("reclaimed") is True)
        return JSONResponse(
            status_code=200 if settled else 409,
            content={"ok": settled, "settled": settled, "active_tasks": active, "reclaim_proof": proof},
        )

    @app.post("/internal/v1/gpu-residency/revoke-lease")
    async def revoke_lease(request: Request) -> JSONResponse:
        if not verify_internal_bearer(request.headers.get("authorization", "")):
            raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="INVALID_JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="INVALID_JSON")
        lease_id = str(body.get("lease_id") or "").strip()
        try:
            fencing_token = int(body.get("fencing_token") or 0)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="INVALID_FENCING_TOKEN") from exc
        try:
            affected = fence_guard.revoke_lease(lease_id, fencing_token)
            await _reconcile_running_tasks()
            proof = fence_guard.reclaim_and_seal(lease_id=lease_id, fencing_token=fencing_token, settled=True)
        except FencePersistenceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        settled = bool(isinstance(proof, dict) and proof.get("reclaimed") is True)
        return JSONResponse(
            status_code=200,
            content={
                "revoked": True,
                "settled": settled,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "affected_tasks": affected,
                "reclaim_proof": proof,
            },
        )

    @app.post("/internal/v1/gpu-residency/evict-vram")
    async def evict_vram(request: Request) -> JSONResponse:
        if not verify_internal_bearer(request.headers.get("authorization", "")):
            raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
        raise HTTPException(status_code=409, detail="SCALE_TO_ZERO_REQUIRED")

    @app.post("/release_task")
    async def release_task(request: Request) -> Response:
        raw_body = await request.body()
        digest = hashlib.sha256(raw_body).hexdigest()
        req_id = str(uuid.uuid4())
        try:
            admitted = fence_guard.begin_proxied_request(
                request.headers,
                request_id=req_id,
                request_digest=digest,
                phase="submitting",
            )
        except FencePersistenceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (StaleFenceError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        dedup_action = str(admitted.get("dedup_action") or "forward") if isinstance(admitted, dict) else "forward"
        if dedup_action == "in_progress":
            return JSONResponse(
                status_code=202,
                content={
                    "code": 202,
                    "status": "in_progress",
                    "error_code": "SUBMISSION_IN_PROGRESS",
                    "request_id": str(admitted.get("request_id") or ""),
                    "upstream_task_id": str(admitted.get("upstream_task_id") or ""),
                },
                headers={"X-VF-Dedup-Replay": "in_progress"},
            )
        if dedup_action == "cached_response":
            cached_bytes = base64.b64decode(str(admitted.get("response_body_b64") or "").encode("ascii"))
            return Response(
                content=cached_bytes,
                status_code=int(admitted.get("response_status") or 200),
                media_type=str(admitted.get("content_type") or "application/json"),
                headers={"X-VF-Dedup-Replay": "cached_response"},
            )

        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length") and not k.lower().startswith("x-vf-gpu-")
        }
        try:
            async with httpx.AsyncClient(transport=transport, timeout=120.0, trust_env=False) as client:
                up_resp = await client.post(f"{resolved_upstream}/release_task", content=raw_body, headers=fwd_headers)
        except httpx.TimeoutException as exc:
            try:
                fence_guard.mark_inflight_uncertain(req_id)
            except FencePersistenceError as pexc:
                raise HTTPException(status_code=503, detail=str(pexc)) from pexc
            raise HTTPException(status_code=504, detail=f"UPSTREAM_TIMEOUT:{exc}") from exc
        except httpx.HTTPError as exc:
            try:
                fence_guard.mark_inflight_uncertain(req_id)
            except FencePersistenceError as pexc:
                raise HTTPException(status_code=503, detail=str(pexc)) from pexc
            raise HTTPException(status_code=502, detail=f"UPSTREAM_TRANSPORT_ERROR:{exc}") from exc
        except BaseException:
            fence_guard.mark_inflight_uncertain(req_id)
            raise

        try:
            if up_resp.status_code >= 500:
                fence_guard.mark_inflight_uncertain(req_id)
            elif 400 <= up_resp.status_code < 500:
                fence_guard.abort_submitting_request(req_id)
            else:
                task_id = ""
                explicit_upstream_reject = False
                if up_resp.content:
                    try:
                        doc = up_resp.json()
                        if isinstance(doc, dict):
                            data = doc.get("data")
                            if isinstance(data, dict) and isinstance(data.get("task_id"), str) and data["task_id"].strip():
                                task_id = data["task_id"].strip()
                            elif doc.get("code") not in (None, 200):
                                explicit_upstream_reject = True
                    except ValueError:
                        pass
                if task_id:
                    fence_guard.bind_upstream_task(
                        req_id,
                        task_id,
                        response_status=up_resp.status_code,
                        response_body=up_resp.content,
                        content_type=up_resp.headers.get("content-type", "application/json"),
                    )
                elif explicit_upstream_reject:
                    fence_guard.abort_submitting_request(req_id)
                else:
                    fence_guard.mark_inflight_uncertain(req_id)
        except FencePersistenceError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        out_headers = {
            k: v
            for k, v in up_resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
        }
        return Response(content=up_resp.content, status_code=up_resp.status_code, headers=out_headers)

    @app.post("/query_result")
    async def query_result(request: Request) -> Response:
        raw_body = await request.body()
        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length") and not k.lower().startswith("x-vf-gpu-")
        }
        async with httpx.AsyncClient(transport=transport, timeout=60.0, trust_env=False) as client:
            up_resp = await client.post(f"{resolved_upstream}/query_result", content=raw_body, headers=fwd_headers)

        proof: dict[str, object] | None = None
        content_bytes = up_resp.content
        if 200 <= up_resp.status_code < 300 and content_bytes:
            try:
                doc = up_resp.json()
                items = doc.get("data") if isinstance(doc, dict) else None
                if isinstance(items, list):
                    settled_tids = [
                        str(item.get("task_id") or "")
                        for item in items
                        if isinstance(item, dict) and item.get("status") in (1, 2) and item.get("task_id")
                    ]
                    if settled_tids:
                        proof = fence_guard.complete_upstream_tasks(settled_tids)
                    if proof is not None and isinstance(doc, dict):
                        doc["reclaim_proof"] = proof
                        content_bytes = json.dumps(doc).encode("utf-8")
            except FencePersistenceError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ValueError:
                pass

        out_headers = {
            k: v
            for k, v in up_resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
        }
        if proof is not None:
            out_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
        return Response(content=content_bytes, status_code=up_resp.status_code, headers=out_headers)

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def passthrough(full_path: str, request: Request) -> Response:
        raw_body = await request.body()
        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length") and not k.lower().startswith("x-vf-gpu-")
        }
        target = f"{resolved_upstream}/{full_path.lstrip('/')}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        async with httpx.AsyncClient(transport=transport, timeout=300.0, trust_env=False) as client:
            up_resp = await client.request(request.method, target, content=raw_body, headers=fwd_headers)
        out_headers = {
            k: v
            for k, v in up_resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
        }
        return Response(content=up_resp.content, status_code=up_resp.status_code, headers=out_headers)

    return app


app = create_acestep_guard_app()
