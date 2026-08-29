from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from .service import AlignmentFailed, AlignmentService, InvalidAlignmentInput, ModelUnavailable


MODEL_ROOT = Path(os.environ.get("LYRICS_ALIGNER_MODEL_ROOT", "/models/whisper"))
MODEL_NAME = os.environ.get("LYRICS_ALIGNER_MODEL", "small")
LANGUAGE = os.environ.get("LYRICS_ALIGNER_LANGUAGE", "zh")
TEMP_ROOT = Path(os.environ.get("LYRICS_ALIGNER_TEMP_ROOT", "/tmp/lyrics-aligner"))
MAX_AUDIO_BYTES = 500 * 1024 * 1024
MAX_LYRICS_BYTES = 1024 * 1024

service = AlignmentService(MODEL_ROOT, MODEL_NAME, LANGUAGE)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        service.load()
    except ModelUnavailable:
        pass
    yield


app = FastAPI(title="VerdantFlare Music Lyrics Aligner API", version="1.0.0", lifespan=lifespan)


def _write_upload(upload: UploadFile, destination: Path, maximum: int) -> None:
    total = 0
    with destination.open("wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > maximum:
                raise HTTPException(status_code=413, detail="uploaded file exceeds limit")
            output.write(chunk)
    if total == 0:
        raise HTTPException(status_code=422, detail="uploaded file is empty")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        status_code=200 if service.ready else 503,
        content={
            "status": "ok" if service.ready else "unavailable",
            "model": service.model_name,
            "language": service.language,
            "backend": "stable-ts-2.19.1",
        },
    )


@app.post("/v1/lyrics/alignments")
def align_lyrics(
    audio: Annotated[UploadFile, File()],
    lyrics: Annotated[UploadFile, File()],
    language: Annotated[str, Form()] = "zh",
) -> Response:
    if language != LANGUAGE:
        raise HTTPException(status_code=422, detail=f"language must be {LANGUAGE}")
    if not service.ready:
        raise HTTPException(status_code=503, detail="alignment model is unavailable")

    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    uploads = (audio, lyrics)
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            audio_path = root / "vocal.input"
            lyrics_path = root / "lyrics.txt"
            _write_upload(audio, audio_path, MAX_AUDIO_BYTES)
            _write_upload(lyrics, lyrics_path, MAX_LYRICS_BYTES)
            lrc = service.align(audio_path, lyrics_path.read_bytes(), root)
    except InvalidAlignmentInput as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ModelUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except AlignmentFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        for upload in uploads:
            upload.file.close()

    return Response(
        content=lrc.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="Aligned_Lyrics.lrc"'},
    )
