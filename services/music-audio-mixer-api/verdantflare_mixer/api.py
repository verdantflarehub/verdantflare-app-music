from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from .lrc import InvalidLRC, validate_lrc
from .service import MasteringFailed, MixerService


TEMP_ROOT = Path(os.environ.get("MIXER_TEMP_ROOT", "/tmp/music-mixer"))
MAX_AUDIO_BYTES = 500 * 1024 * 1024
MAX_LRC_BYTES = 1024 * 1024
service = MixerService()
app = FastAPI(title="VerdantFlare Music Audio Mixer API", version="1.0.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.post("/v1/audio/masters")
def master_audio(
    instrumental: Annotated[UploadFile, File()],
    vocal: Annotated[UploadFile, File()],
    lyrics_lrc: Annotated[UploadFile, File()],
    bpm: Annotated[float, Form(ge=40.0, le=240.0)],
) -> Response:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    uploads = (instrumental, vocal, lyrics_lrc)
    try:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            instrumental_path = root / "instrumental.input"
            vocal_path = root / "vocal.input"
            lrc_path = root / "lyrics.lrc"
            _write_upload(instrumental, instrumental_path, MAX_AUDIO_BYTES)
            _write_upload(vocal, vocal_path, MAX_AUDIO_BYTES)
            _write_upload(lyrics_lrc, lrc_path, MAX_LRC_BYTES)
            lrc_text = validate_lrc(lrc_path.read_bytes())
            payload = service.master(
                instrumental=instrumental_path,
                vocal=vocal_path,
                lrc_text=lrc_text,
                bpm=bpm,
                work_directory=root,
            ).read_bytes()
    except InvalidLRC as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except MasteringFailed as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    finally:
        for upload in uploads:
            upload.file.close()

    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="Final_Song.zip"'},
    )
