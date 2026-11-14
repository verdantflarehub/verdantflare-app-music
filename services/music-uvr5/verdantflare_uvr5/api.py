from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from .service import DEREVERB_MODEL, SEPARATION_MODEL, SeparationFailed, UVR5Service


MODEL_ROOT = Path(os.environ.get("UVR5_MODEL_ROOT", "/models/audio-separator"))
TEMP_ROOT = Path(os.environ.get("UVR5_TEMP_ROOT", "/tmp/uvr5"))
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

service = UVR5Service(
    MODEL_ROOT,
    backing_model=os.environ.get("UVR5_BACKING_MODEL_FILENAME") or None,
    backing_lead_stem=os.environ.get("UVR5_BACKING_LEAD_STEM") or None,
    backing_vocal_stem=os.environ.get("UVR5_BACKING_VOCAL_STEM") or None,
)
app = FastAPI(title="VerdantFlare Music UVR5 API", version="1.0.0")


@app.get("/health")
def health() -> JSONResponse:
    cuda = torch.cuda.is_available()
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
        },
    )


@app.post("/v1/audio/stem-separations")
def separate_stems(audio: Annotated[UploadFile, File()]) -> Response:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            source = root / "input.audio"
            total = 0
            with source.open("wb") as output:
                while chunk := audio.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="audio exceeds 500 MiB")
                    output.write(chunk)
            if total == 0:
                raise HTTPException(status_code=422, detail="audio is empty")
            payload = service.separate(source, root).read_bytes()
    except SeparationFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        audio.file.close()

    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="stems.zip"'},
    )
