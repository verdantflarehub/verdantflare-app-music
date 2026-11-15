from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Annotated, Literal

import soundfile as sf
try:
    import torch
except ImportError:  # pragma: no cover - CPU unit-test fallback
    torch = None  # type: ignore[assignment]

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .catalog import InvalidModelId, VoiceModelCatalog, VoiceModelNotFound
from .gpu_guard import (
    DuplicateInProgressError,
    FencePersistenceError,
    LeaseFenceGuard,
    StaleFenceError,
    read_runtime_identity,
    verify_internal_bearer,
)
from .preparation import PreparationFailed, PreparationSource, prepare_voice_dataset_archive
from .service import ConversionFailed, RVCService
from .training import RVCTrainer, TrainingFailed


VOICE_ROOT = Path(os.environ.get("RVC_VOICE_ROOT", "/models/rvc/voices"))
RUNTIME_ROOT = Path(os.environ.get("RVC_RUNTIME_ROOT", "/models/rvc/runtime"))
TEMP_ROOT = Path(os.environ.get("RVC_TEMP_ROOT", "/tmp/rvc"))
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PREPARATION_BYTES = 300 * 1024 * 1024


def _cuda_available() -> bool:
    return bool(torch is not None and torch.cuda.is_available())


catalog = VoiceModelCatalog(VOICE_ROOT)
service = RVCService(catalog)
trainer = RVCTrainer(catalog=catalog, runtime_root=RUNTIME_ROOT)
preparation_lock = threading.Lock()
fence_guard = LeaseFenceGuard(
    state_path=os.environ.get("VF_GPU_GUARD_STATE_PATH", "").strip() or str(TEMP_ROOT / "gpu_guard_state.json")
)
_active_lock = threading.Lock()
_active_tasks = 0
app = FastAPI(title="VerdantFlare Music RVC API", version="1.0.0")


def _active_count() -> int:
    with _active_lock:
        ext = _active_tasks
    return max(ext, fence_guard.active_inflight_count)


class _ActiveTaskScope:
    def __enter__(self) -> None:
        global _active_tasks
        with _active_lock:
            _active_tasks += 1

    def __exit__(self, exc_type, exc, tb) -> bool:
        global _active_tasks
        with _active_lock:
            _active_tasks = max(0, _active_tasks - 1)
        return False


@app.get("/health")
def health() -> JSONResponse:
    runtime_files = {
        "hubert": (RUNTIME_ROOT / "hubert_base.pt").is_file(),
        "rmvpe": (RUNTIME_ROOT / "rmvpe.pt").is_file(),
    }
    cuda_available = _cuda_available()
    ready = cuda_available and all(runtime_files.values())
    active = _active_count()
    residency = "S1" if active > 0 else ("S2" if service.is_loaded else "S3")
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "unavailable",
            "cuda": cuda_available,
            "runtime": runtime_files,
            "models": len(catalog.list_models()),
            "active_tasks": active,
            "residency_state": residency,
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
        with _active_lock:
            ext_active = _active_tasks
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
    with _active_lock:
        ext_active = _active_tasks
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
    if _active_count() > 0 or not preparation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="BUSY_S1_INFERENCE")
    try:
        if _active_count() > 0 or not service.evict_idle_vram():
            raise HTTPException(status_code=409, detail="BUSY_S1_INFERENCE")
    finally:
        preparation_lock.release()
    return JSONResponse(
        status_code=200,
        content={
            "evicted": True,
            "residency_state": "S3",
            "active_tasks": _active_count(),
            "runtime_identity": read_runtime_identity(),
        },
    )


@app.get("/v1/voice-models")
def list_voice_models() -> dict[str, list[dict[str, object]]]:
    return {
        "data": [
            {
                "id": model.model_id,
                "has_index": model.index_path is not None,
            }
            for model in catalog.list_models()
        ]
    }


def _write_upload(upload: UploadFile, destination: Path, maximum_bytes: int) -> tuple[int, str]:
    total = 0
    digest = hashlib.sha256()
    with destination.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > maximum_bytes:
                raise HTTPException(status_code=413, detail="audio exceeds upload limit")
            output.write(chunk)
            digest.update(chunk)
    if total == 0:
        raise HTTPException(status_code=422, detail="audio is empty")
    return total, digest.hexdigest()


def _safe_upload_name(upload: UploadFile, number: int) -> str:
    value = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not value or len(value.encode("utf-8")) > 255:
        return f"recording-{number:02d}.audio"
    return value


@app.post("/v1/voice-datasets/prepare")
def prepare_voice_dataset(request: Request, audio: Annotated[list[UploadFile], File()]) -> Response:
    if not 1 <= len(audio) <= 20:
        raise HTTPException(status_code=422, detail="voice preparation requires 1-20 recordings")
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    request_id = str(uuid.uuid4())
    grant: dict[str, object] | None = None
    settled = False
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            root = Path(temporary_directory)
            sources: list[PreparationSource] = []
            total_bytes = 0
            combined_digest = hashlib.sha256()
            for number, upload in enumerate(audio, start=1):
                input_path = root / f"input-{number:02d}.audio"
                size, digest = _write_upload(
                    upload,
                    input_path,
                    MAX_PREPARATION_BYTES - total_bytes,
                )
                total_bytes += size
                combined_digest.update(digest.encode("ascii"))
                sources.append(
                    PreparationSource(
                        filename=_safe_upload_name(upload, number),
                        path=input_path,
                        sha256=digest,
                    )
                )
            grant = fence_guard.extract_and_validate(
                request.headers,
                request_id=request_id,
                request_digest=combined_digest.hexdigest(),
            )
            fence_guard.verify_grant_live(grant)
            archive_path = root / "voice-preparation.zip"
            with _ActiveTaskScope(), preparation_lock, ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
            ) as executor:
                executor.submit(
                    prepare_voice_dataset_archive,
                    sources,
                    root / "work",
                    archive_path,
                ).result()
            payload = archive_path.read_bytes()
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
    except PreparationFailed as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        for upload in audio:
            upload.file.close()
        with _active_lock:
            ext_active = _active_tasks
        try:
            proof = fence_guard.finish_grant(grant, request_id=request_id, active_tasks=ext_active, settled=settled)
        except FencePersistenceError:
            proof = None

    resp_headers = {"Content-Disposition": 'attachment; filename="voice-preparation.zip"'}
    if proof is not None:
        resp_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
    return Response(
        content=payload,
        media_type="application/zip",
        headers=resp_headers,
    )


@app.post("/v1/voice-models/train")
def train_voice_model(
    request: Request,
    audio: Annotated[UploadFile, File()],
    model_id: Annotated[str, Form(min_length=1, max_length=64)],
    epochs: Annotated[int, Form(ge=10, le=1000)] = 200,
    batch_size: Annotated[int, Form(ge=1, le=16)] = 4,
    save_every_epochs: Annotated[int, Form(ge=1, le=1000)] = 50,
) -> Response:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path: Path | None = None
    trained_model_directory: Path | None = None
    request_id = str(uuid.uuid4())
    grant: dict[str, object] | None = None
    settled = False
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            input_path = temporary_root / "training.audio"
            _, audio_sha = _write_upload(audio, input_path, 500 * 1024 * 1024)
            req_digest = hashlib.sha256(f"{audio_sha}:{model_id}:{epochs}:{batch_size}:{save_every_epochs}".encode("utf-8")).hexdigest()
            grant = fence_guard.extract_and_validate(
                request.headers,
                request_id=request_id,
                request_digest=req_digest,
            )
            fence_guard.verify_grant_live(grant)
            with _ActiveTaskScope():
                result = trainer.train(
                    model_id=model_id,
                    source_audio=input_path,
                    epochs=epochs,
                    batch_size=batch_size,
                    save_every_epochs=save_every_epochs,
                )
                archive_path = result.archive_path
                trained_model_directory = result.checkpoint_path.parent

                validation_input = temporary_root / "validation-input.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(input_path),
                        "-t",
                        "15",
                        "-ac",
                        "1",
                        "-ar",
                        "40000",
                        str(validation_input),
                    ],
                    check=True,
                )
                sample_rate, converted_audio = service.convert(
                    model_id=model_id,
                    input_path=validation_input,
                    speaker_id=0,
                    pitch_shift=0,
                    f0_method="rmvpe",
                    index_rate=0.66,
                    filter_radius=3,
                    resample_sr=0,
                    rms_mix_rate=1.0,
                    protect=0.33,
                )
                validation_output = temporary_root / f"{model_id}_validation.wav"
                sf.write(validation_output, converted_audio, sample_rate, subtype="PCM_16")
                trainer.add_validation_audio(archive_path, validation_output, model_id)
                payload = archive_path.read_bytes()
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
    except InvalidModelId as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (StaleFenceError, ValueError, TrainingFailed) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ConversionFailed, subprocess.CalledProcessError) as error:
        if trained_model_directory is not None:
            import shutil

            shutil.rmtree(trained_model_directory, ignore_errors=True)
        raise HTTPException(status_code=500, detail="model validation audio failed") from error
    finally:
        audio.file.close()
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)
        with _active_lock:
            ext_active = _active_tasks
        try:
            proof = fence_guard.finish_grant(grant, request_id=request_id, active_tasks=ext_active, settled=settled)
        except FencePersistenceError:
            proof = None

    resp_headers = {"Content-Disposition": f'attachment; filename="{model_id}.zip"'}
    if proof is not None:
        resp_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
    return Response(
        content=payload,
        media_type="application/zip",
        headers=resp_headers,
    )


@app.post("/v1/audio/voice-conversions")
def convert_voice(
    request: Request,
    audio: Annotated[UploadFile, File()],
    model_id: Annotated[str, Form(min_length=1, max_length=64)],
    speaker_id: Annotated[int, Form(ge=0)] = 0,
    pitch_shift: Annotated[int, Form(ge=-24, le=24)] = 0,
    f0_method: Annotated[Literal["rmvpe", "harvest", "pm", "crepe"], Form()] = "rmvpe",
    index_rate: Annotated[float, Form(ge=0.0, le=1.0)] = 0.66,
    filter_radius: Annotated[int, Form(ge=0, le=7)] = 3,
    resample_sr: Annotated[int, Form(ge=0, le=48000)] = 0,
    rms_mix_rate: Annotated[float, Form(ge=0.0, le=1.0)] = 1.0,
    protect: Annotated[float, Form(ge=0.0, le=0.5)] = 0.33,
) -> Response:
    if resample_sr not in (0,) and resample_sr < 16000:
        raise HTTPException(status_code=422, detail="resample_sr must be 0 or 16000-48000")

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    request_id = str(uuid.uuid4())
    grant: dict[str, object] | None = None
    settled = False
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            input_path = Path(temporary_directory) / "input.audio"
            _, audio_sha = _write_upload(audio, input_path, MAX_UPLOAD_BYTES)
            req_digest = hashlib.sha256(
                f"{audio_sha}:{model_id}:{speaker_id}:{pitch_shift}:{f0_method}:{index_rate}:{filter_radius}:{resample_sr}:{rms_mix_rate}:{protect}".encode("utf-8")
            ).hexdigest()
            grant = fence_guard.extract_and_validate(
                request.headers,
                request_id=request_id,
                request_digest=req_digest,
            )
            fence_guard.verify_grant_live(grant)
            with _ActiveTaskScope():
                sample_rate, converted_audio = service.convert(
                    model_id=model_id,
                    input_path=input_path,
                    speaker_id=speaker_id,
                    pitch_shift=pitch_shift,
                    f0_method=f0_method,
                    index_rate=index_rate,
                    filter_radius=filter_radius,
                    resample_sr=resample_sr,
                    rms_mix_rate=rms_mix_rate,
                    protect=protect,
                )
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
    except InvalidModelId as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except VoiceModelNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (StaleFenceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ConversionFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        audio.file.close()
        with _active_lock:
            ext_active = _active_tasks
        try:
            proof = fence_guard.finish_grant(grant, request_id=request_id, active_tasks=ext_active, settled=settled)
        except FencePersistenceError:
            proof = None

    output = io.BytesIO()
    sf.write(output, converted_audio, sample_rate, format="WAV", subtype="PCM_16")
    resp_headers = {
        "Content-Disposition": 'attachment; filename="converted.wav"',
        "X-RVC-Model": model_id,
    }
    if proof is not None:
        resp_headers["X-VF-Reclaim-Proof"] = json.dumps(proof, separators=(",", ":"))
    return Response(
        content=output.getvalue(),
        media_type="audio/wav",
        headers=resp_headers,
    )
