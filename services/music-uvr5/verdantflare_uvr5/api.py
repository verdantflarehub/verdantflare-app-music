from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated

try:
    import torch
except ImportError:  # pragma: no cover - CPU unit-test fallback
    torch = None  # type: ignore[assignment]

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .gpu_guard import (
    DuplicateInProgressError,
    FencePersistenceError,
    LeaseFenceGuard,
    StaleFenceError,
    read_runtime_identity,
    verify_internal_bearer,
)
from .service import DEREVERB_MODEL, SEPARATION_MODEL, SeparationFailed, UVR5Service


MODEL_ROOT = Path(os.environ.get("UVR5_MODEL_ROOT", "/models/audio-separator"))
TEMP_ROOT = Path(os.environ.get("UVR5_TEMP_ROOT", "/tmp/uvr5"))
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def _cuda_available() -> bool:
    return bool(torch is not None and torch.cuda.is_available())


service = UVR5Service(
    MODEL_ROOT,
    backing_model=os.environ.get("UVR5_BACKING_MODEL_FILENAME") or None,
    backing_lead_stem=os.environ.get("UVR5_BACKING_LEAD_STEM") or None,
    backing_vocal_stem=os.environ.get("UVR5_BACKING_VOCAL_STEM") or None,
)
fence_guard = LeaseFenceGuard(
    state_path=os.environ.get("VF_GPU_GUARD_STATE_PATH", "").strip() or str(TEMP_ROOT / "gpu_guard_state.json")
)
app = FastAPI(title="VerdantFlare Music UVR5 API", version="1.0.0")


@app.get("/health")
def health() -> JSONResponse:
    cuda = _cuda_available()
    active = max(service.active_tasks, fence_guard.active_inflight_count)
    return JSONResponse(
        status_code=200 if cuda else 503,
        content={
            "status": "ok" if cuda else "unavailable",
            "cuda": cuda,
            "models": {
                "separation": SEPARATION_MODEL,
                "dereverb": DEREVERB_MODEL,
                "backing": service.backing_model,
            },
            "backing_ready": service.backing_model is not None and (MODEL_ROOT / service.backing_model).is_file(),
            "active_tasks": active,
            "residency_state": "S1" if active > 0 else "S3",
            "runtime_identity": read_runtime_identity(),
        },
    )


@app.post("/internal/v1/gpu-residency/revoke-lease")
async def revoke_gpu_lease(request: Request) -> JSONResponse:
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
        ext_active = service.active_tasks
        proof = fence_guard.reclaim_and_seal(
            lease_id=lease_id,
            fencing_token=fencing_token,
            active_tasks=ext_active,
            settled=ext_active == 0,
        )
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


@app.post("/internal/v1/gpu-guard/reclaim")
async def reclaim_gpu_guard(request: Request) -> JSONResponse:
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
    ext_active = service.active_tasks
    try:
        proof = fence_guard.reclaim_and_seal(
            lease_id=lease_id,
            fencing_token=fencing_token,
            active_tasks=ext_active,
            settled=ext_active == 0,
        )
    except FencePersistenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    active = int(proof["active_tasks"]) if isinstance(proof, dict) else ext_active
    settled = bool(isinstance(proof, dict) and proof.get("reclaimed") is True)
    return JSONResponse(
        status_code=200 if settled else 409,
        content={
            "ok": settled,
            "settled": settled,
            "active_tasks": active,
            "reclaim_proof": proof,
        },
    )


@app.post("/internal/v1/gpu-residency/evict-vram")
async def evict_gpu_vram(request: Request) -> JSONResponse:
    if not verify_internal_bearer(request.headers.get("authorization", "")):
        raise HTTPException(status_code=401, detail="UNAUTHENTICATED")
    if service.active_tasks > 0 or fence_guard.active_inflight_count > 0 or not service.evict_idle_vram():
        raise HTTPException(status_code=409, detail="BUSY_S1_INFERENCE")
    return JSONResponse(
        status_code=200,
        content={
            "evicted": True,
            "residency_state": "S3",
            "active_tasks": max(service.active_tasks, fence_guard.active_inflight_count),
            "runtime_identity": read_runtime_identity(),
        },
    )


@app.post("/v1/audio/stem-separations")
def separate_stems(request: Request, audio: Annotated[UploadFile, File()]) -> Response:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    request_id = str(uuid.uuid4())
    grant: dict[str, object] | None = None
    settled = False
    proof: dict[str, object] | None = None
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            source = root / "input.audio"
            total = 0
            digest = hashlib.sha256()
            with source.open("wb") as output:
                while chunk := audio.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="audio exceeds 500 MiB")
                    output.write(chunk)
                    digest.update(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail="audio is empty")
            grant = fence_guard.extract_and_validate(
                request.headers,
                request_id=request_id,
                request_digest=digest.hexdigest(),
            )
            fence_guard.verify_grant_live(grant)
            payload = service.separate(source, root).read_bytes()
            settled = True
    except DuplicateInProgressError as dup:
        return JSONResponse(
            status_code=202,
            content={
                "status": "in_progress",
                "error_code": "SUBMISSION_IN_PROGRESS",
                "request_id": dup.request_id,
            },
            headers={"X-VF-Dedup-Replay": "in_progress"},
        )
    except FencePersistenceError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (StaleFenceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except SeparationFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        audio.file.close()
        try:
            proof = fence_guard.finish_grant(
                grant,
                request_id=request_id,
                active_tasks=service.active_tasks,
                settled=settled,
            )
        except FencePersistenceError:
            proof = None

    resp_headers = {"Content-Disposition": 'attachment; filename="stems.zip"'}
    if proof is not None:
        resp_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
    return Response(
        content=payload,
        media_type="application/zip",
        headers=resp_headers,
    )
