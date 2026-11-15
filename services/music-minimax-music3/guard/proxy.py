"""ASGI receiver-guard proxy in front of sgl-omni serve (127.0.0.1:18000) for MiniMax Music 3."""
from __future__ import annotations

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


def create_music3_guard_app(
    *,
    upstream_url: str | None = None,
    state_path: str | Path | None = None,
    min_cards: int = 2,
    init_pid: int = 0,
    upstream_leader_pid: int = 0,
    upstream_pid_file: str | Path | None = None,
    proc_root: str | Path = "/proc",
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    resolved_upstream = (upstream_url or os.environ.get("MUSIC3_UPSTREAM_URL", "http://127.0.0.1:18000")).rstrip("/")
    resolved_state = state_path or os.environ.get("VF_GPU_GUARD_STATE_PATH", "/tmp/music3-guard/gpu_guard_state.json")
    fence_guard = LeaseFenceGuard(
        min_cards=min_cards,
        state_path=resolved_state,
        init_pid=init_pid,
        upstream_leader_pid=upstream_leader_pid,
        upstream_pid_file=upstream_pid_file,
        proc_root=proc_root,
    )

    app = FastAPI(title="VerdantFlare MiniMax Music 3 Guard Proxy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.fence_guard = fence_guard

    @app.get("/health")
    async def health() -> JSONResponse:
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
        try:
            async with httpx.AsyncClient(transport=transport, timeout=5.0, trust_env=False) as client:
                up = await client.get(f"{resolved_upstream}/health")
        except httpx.HTTPError:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "active_tasks": active,
                    "residency_state": "S3",
                    "runtime_identity": ident,
                },
            )
        base: dict[str, object] = {}
        if up.content:
            try:
                parsed = up.json()
                if isinstance(parsed, dict):
                    base.update(parsed)
            except ValueError:
                pass
        ok = 200 <= up.status_code < 300
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
        # sgl-omni holds static 2-GPU weights until Deployment scales to 0; never claim in-place VRAM eviction.
        raise HTTPException(status_code=409, detail="SCALE_TO_ZERO_REQUIRED")

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(full_path: str, request: Request) -> Response:
        raw_body = await request.body()
        is_mutating = request.method.upper() in ("POST", "PUT", "PATCH")
        req_id = str(uuid.uuid4())
        admitted: dict[str, object] | None = None
        if is_mutating:
            digest = hashlib.sha256(raw_body).hexdigest()
            try:
                admitted = fence_guard.begin_proxied_request(
                    request.headers,
                    request_id=req_id,
                    request_digest=digest,
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
                        "status": "in_progress",
                        "error_code": "SUBMISSION_IN_PROGRESS",
                        "request_id": str(admitted.get("request_id") or ""),  # type: ignore[union-attr]
                    },
                    headers={"X-VF-Dedup-Replay": "in_progress"},
                )
            if dedup_action == "cached_response" and isinstance(admitted, dict):
                cached_headers = {"X-VF-Dedup-Replay": "cached_response"}
                cached_proof = admitted.get("reclaim_proof")
                if isinstance(cached_proof, dict):
                    cached_headers["X-VF-Reclaim-Proof"] = json.dumps(cached_proof, separators=(",", ":"))
                return Response(
                    content=bytes(admitted.get("response_bytes") or b""),  # type: ignore[arg-type]
                    status_code=int(admitted.get("response_status") or 200),
                    media_type=str(admitted.get("content_type") or "application/octet-stream"),
                    headers=cached_headers,
                )

        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length") and not k.lower().startswith("x-vf-gpu-")
        }
        target = f"{resolved_upstream}/{full_path.lstrip('/')}"
        if request.url.query:
            target = f"{target}?{request.url.query}"

        try:
            async with httpx.AsyncClient(transport=transport, timeout=1800.0, trust_env=False) as client:
                up_resp = await client.request(request.method, target, content=raw_body, headers=fwd_headers)
        except httpx.TimeoutException as exc:
            if is_mutating:
                try:
                    fence_guard.mark_inflight_uncertain(req_id)
                except FencePersistenceError as pexc:
                    raise HTTPException(status_code=503, detail=str(pexc)) from pexc
            raise HTTPException(status_code=504, detail=f"UPSTREAM_TIMEOUT:{exc}") from exc
        except httpx.HTTPError as exc:
            if is_mutating:
                try:
                    fence_guard.mark_inflight_uncertain(req_id)
                except FencePersistenceError as pexc:
                    raise HTTPException(status_code=503, detail=str(pexc)) from pexc
            raise HTTPException(status_code=502, detail=f"UPSTREAM_TRANSPORT_ERROR:{exc}") from exc
        except BaseException:
            if is_mutating:
                fence_guard.mark_inflight_uncertain(req_id)
            raise

        proof: dict[str, object] | None = None
        if is_mutating:
            try:
                if up_resp.status_code >= 500:
                    fence_guard.mark_inflight_uncertain(req_id)
                else:
                    proof = fence_guard.complete_proxied_request(
                        req_id,
                        settled=200 <= up_resp.status_code < 500,
                        response_status=up_resp.status_code,
                        response_body=up_resp.content,
                        content_type=up_resp.headers.get("content-type", "application/octet-stream"),
                    )
            except FencePersistenceError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        out_headers = {
            k: v
            for k, v in up_resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "content-encoding", "connection")
        }
        if proof is not None:
            out_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
        return Response(content=up_resp.content, status_code=up_resp.status_code, headers=out_headers)

    return app


app = create_music3_guard_app()
