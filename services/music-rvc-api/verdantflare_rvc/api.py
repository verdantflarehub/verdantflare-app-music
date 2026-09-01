from __future__ import annotations

import hashlib
import io
import multiprocessing
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Annotated, Literal

import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from .catalog import InvalidModelId, VoiceModelCatalog, VoiceModelNotFound
from .preparation import PreparationFailed, PreparationSource, prepare_voice_dataset_archive
from .service import ConversionFailed, RVCService
from .training import RVCTrainer, TrainingFailed


VOICE_ROOT = Path(os.environ.get("RVC_VOICE_ROOT", "/models/rvc/voices"))
RUNTIME_ROOT = Path(os.environ.get("RVC_RUNTIME_ROOT", "/models/rvc/runtime"))
TEMP_ROOT = Path(os.environ.get("RVC_TEMP_ROOT", "/tmp/rvc"))
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PREPARATION_BYTES = 300 * 1024 * 1024

catalog = VoiceModelCatalog(VOICE_ROOT)
service = RVCService(catalog)
trainer = RVCTrainer(catalog=catalog, runtime_root=RUNTIME_ROOT)
preparation_lock = threading.Lock()
app = FastAPI(title="VerdantFlare Music RVC API", version="1.0.0")


@app.get("/health")
def health() -> JSONResponse:
    runtime_files = {
        "hubert": (RUNTIME_ROOT / "hubert_base.pt").is_file(),
        "rmvpe": (RUNTIME_ROOT / "rmvpe.pt").is_file(),
    }
    cuda_available = torch.cuda.is_available()
    ready = cuda_available and all(runtime_files.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "unavailable",
            "cuda": cuda_available,
            "runtime": runtime_files,
            "models": len(catalog.list_models()),
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
def prepare_voice_dataset(audio: Annotated[list[UploadFile], File()]) -> Response:
    if not 1 <= len(audio) <= 20:
        raise HTTPException(status_code=422, detail="voice preparation requires 1-20 recordings")
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            root = Path(temporary_directory)
            sources: list[PreparationSource] = []
            total_bytes = 0
            for number, upload in enumerate(audio, start=1):
                input_path = root / f"input-{number:02d}.audio"
                size, digest = _write_upload(
                    upload,
                    input_path,
                    MAX_PREPARATION_BYTES - total_bytes,
                )
                total_bytes += size
                sources.append(
                    PreparationSource(
                        filename=_safe_upload_name(upload, number),
                        path=input_path,
                        sha256=digest,
                    )
                )
            archive_path = root / "voice-preparation.zip"
            with preparation_lock, ProcessPoolExecutor(
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
    except PreparationFailed as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    finally:
        for upload in audio:
            upload.file.close()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="voice-preparation.zip"'},
    )


@app.post("/v1/voice-models/train")
def train_voice_model(
    audio: Annotated[UploadFile, File()],
    model_id: Annotated[str, Form(min_length=1, max_length=64)],
    epochs: Annotated[int, Form(ge=10, le=1000)] = 200,
    batch_size: Annotated[int, Form(ge=1, le=16)] = 4,
    save_every_epochs: Annotated[int, Form(ge=1, le=1000)] = 50,
) -> Response:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    archive_path: Path | None = None
    trained_model_directory: Path | None = None
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            temporary_root = Path(temporary_directory)
            input_path = temporary_root / "training.audio"
            _write_upload(audio, input_path, 500 * 1024 * 1024)
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
    except InvalidModelId as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except TrainingFailed as error:
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

    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{model_id}.zip"'},
    )


@app.post("/v1/audio/voice-conversions")
def convert_voice(
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
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as temporary_directory:
            input_path = Path(temporary_directory) / "input.audio"
            _write_upload(audio, input_path, MAX_UPLOAD_BYTES)

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
    except InvalidModelId as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except VoiceModelNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ConversionFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        audio.file.close()

    output = io.BytesIO()
    sf.write(output, converted_audio, sample_rate, format="WAV", subtype="PCM_16")
    return Response(
        content=output.getvalue(),
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'attachment; filename="converted.wav"',
            "X-RVC-Model": model_id,
        },
    )
