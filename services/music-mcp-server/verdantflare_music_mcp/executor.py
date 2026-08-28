from __future__ import annotations

import io
import math
import os
import re
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactRecord,
    ArtifactStore,
    require_filename,
    require_project_id,
)


class ExecutionError(RuntimeError):
    pass


MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
AUDIO_MEDIA_TYPES = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
}


def require_model_id(model_id: str) -> str:
    value = model_id.strip()
    if not MODEL_ID_PATTERN.fullmatch(value):
        raise ValueError("model_id must contain 1-64 ASCII letters, digits, dots, underscores, or hyphens")
    return value


def require_sha256(value: str) -> str:
    digest = value.strip().lower()
    if not SHA256_PATTERN.fullmatch(digest):
        raise ValueError("expected_sha256 must contain exactly 64 hexadecimal characters")
    return digest


def require_audio_filename(filename: str) -> tuple[str, str]:
    safe_filename = require_filename(filename)
    media_type = AUDIO_MEDIA_TYPES.get(Path(safe_filename).suffix.lower())
    if media_type is None:
        raise ValueError("filename must use a supported audio extension")
    return safe_filename, media_type


def normalized_https_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("asset import origins must be absolute HTTPS origins")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("asset import origin has an invalid port") from error
    host = parsed.hostname.lower()
    return f"https://{host}" if port in {None, 443} else f"https://{host}:{port}"


def parse_asset_import_origins(value: str) -> frozenset[str]:
    return frozenset(normalized_https_origin(item) for item in value.split(",") if item.strip())


def require_import_url(source_url: str, allowed_origins: frozenset[str]) -> None:
    if not allowed_origins:
        raise ExecutionError("S3 asset import is disabled")
    if len(source_url) > 4096:
        raise ValueError("source_url exceeds 4096 characters")
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path
        or parsed.path == "/"
        or parsed.fragment
    ):
        raise ValueError("source_url must be an absolute HTTPS object URL without credentials or a fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("source_url has an invalid port") from error
    host = parsed.hostname.lower()
    origin = f"https://{host}" if port in {None, 443} else f"https://{host}:{port}"
    if origin not in allowed_origins:
        raise ValueError("source_url origin is not allowed")


@dataclass(frozen=True)
class ServiceURLs:
    music3: str
    uvr5: str
    rvc: str
    mixer: str

    @classmethod
    def from_environment(cls) -> "ServiceURLs":
        return cls(
            music3=os.environ.get("MUSIC3_URL", "http://music-minimax-music3-api:8000").rstrip("/"),
            uvr5=os.environ.get("UVR5_URL", "http://music-uvr5-api:8000").rstrip("/"),
            rvc=os.environ.get("RVC_URL", "http://music-rvc-api:8000").rstrip("/"),
            mixer=os.environ.get("MIXER_URL", "http://music-audio-mixer-api:8000").rstrip("/"),
        )


def trim_pcm_wav(payload: bytes, duration_seconds: float) -> bytes:
    if not 1.0 <= duration_seconds <= 300.0:
        raise ValueError("duration_seconds must be between 1 and 300")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ExecutionError("Music3 returned a compressed WAV")
            target_frames = round(duration_seconds * source.getframerate())
            if source.getnframes() < target_frames:
                raise ExecutionError("Music3 ended before the requested exact duration")
            parameters = source.getparams()
            frames = source.readframes(target_frames)
    except (EOFError, wave.Error) as error:
        raise ExecutionError("Music3 returned an invalid WAV") from error

    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(parameters.nchannels)
        destination.setsampwidth(parameters.sampwidth)
        destination.setframerate(parameters.framerate)
        destination.writeframes(frames)
    return output.getvalue()


def extract_expected_zip(payload: bytes, expected: dict[str, str]) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members if member.filename != "manifest.json"]
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise ExecutionError("downstream ZIP does not contain the exact expected files")
            if sum(member.file_size for member in members) > 1024 * 1024 * 1024:
                raise ExecutionError("downstream ZIP contents exceed 1 GiB")
            outputs: dict[str, bytes] = {}
            for member in members:
                if member.filename == "manifest.json":
                    continue
                if member.is_dir() or member.flag_bits & 0x1 or require_filename(member.filename) != member.filename:
                    raise ExecutionError("downstream ZIP contains an unsafe member")
                if member.file_size > 1024 * 1024 * 1024:
                    raise ExecutionError("downstream ZIP member exceeds 1 GiB")
                outputs[member.filename] = archive.read(member)
            return outputs
    except (ValueError, zipfile.BadZipFile) as error:
        raise ExecutionError("downstream returned an invalid ZIP") from error


class MusicExecutor:
    def __init__(
        self,
        store: ArtifactStore,
        service_urls: ServiceURLs,
        client: httpx.Client | None = None,
        asset_import_origins: frozenset[str] | None = None,
    ) -> None:
        self.store = store
        self.service_urls = service_urls
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=3600.0, write=600.0, pool=10.0),
            follow_redirects=False,
        )
        self.asset_import_origins = (
            parse_asset_import_origins(os.environ.get("MUSIC_ASSET_IMPORT_ORIGINS", ""))
            if asset_import_origins is None
            else asset_import_origins
        )

    def _post(self, service: str, url: str, **kwargs: object) -> bytes:
        try:
            response = self.client.post(url, **kwargs)
        except httpx.HTTPError as error:
            raise ExecutionError(f"{service} request failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ExecutionError(f"{service} returned HTTP {response.status_code}")
        if not response.content:
            raise ExecutionError(f"{service} returned an empty response")
        return response.content

    def import_asset(
        self,
        *,
        project_id: str,
        source_url: str,
        filename: str,
        expected_sha256: str,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        safe_filename, media_type = require_audio_filename(filename)
        digest = require_sha256(expected_sha256)
        require_import_url(source_url, self.asset_import_origins)
        try:
            with self.client.stream(
                "GET",
                source_url,
                headers={"Accept": "audio/*, application/octet-stream"},
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ExecutionError(f"S3 asset import returned HTTP {response.status_code}")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as error:
                        raise ExecutionError("S3 asset import returned an invalid Content-Length") from error
                    if declared_size <= 0 or declared_size > MAX_ARTIFACT_BYTES:
                        raise ExecutionError("S3 asset import size must be between 1 byte and 1 GiB")
                record = self.store.create_from_chunks(
                    project_id=project,
                    operation="asset.import",
                    filename=safe_filename,
                    media_type=media_type,
                    chunks=response.iter_bytes(),
                    expected_sha256=digest,
                )
        except ArtifactError:
            raise
        except httpx.HTTPError as error:
            raise ExecutionError("S3 asset import request failed") from error
        return [record]

    def generate(
        self,
        *,
        project_id: str,
        lyrics: str,
        instructions: str,
        candidate_number: int,
        seed: int,
        duration_seconds: float,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if not lyrics.strip() or not instructions.strip():
            raise ValueError("lyrics and instructions are required")
        if not 1 <= candidate_number <= 99:
            raise ValueError("candidate_number must be between 1 and 99")
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed must be between 0 and 2147483647")
        if not 1.0 <= duration_seconds <= 300.0:
            raise ValueError("duration_seconds must be between 1 and 300")

        max_new_tokens = math.ceil(duration_seconds * 25)
        raw = self._post(
            "Music3",
            f"{self.service_urls.music3}/v1/audio/speech",
            json={
                "model": "MiniMaxAI/MiniMax-Music3",
                "input": lyrics,
                "instructions": instructions,
                "seed": seed,
                "max_new_tokens": max_new_tokens,
                "response_format": "wav",
                "stream": False,
            },
        )
        exact = trim_pcm_wav(raw, duration_seconds)
        prefix = f"Demo_Candidate_{candidate_number}"
        return [
            self.store.create(
                project_id=project,
                operation="music.generate",
                filename=f"{prefix}.generated.wav",
                media_type="audio/wav",
                payload=raw,
            ),
            self.store.create(
                project_id=project,
                operation="music.generate",
                filename=f"{prefix}.wav",
                media_type="audio/wav",
                payload=exact,
            ),
        ]

    def separate_stems(self, *, project_id: str, audio_asset_id: str) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        source, audio = self.store.read(audio_asset_id, project)
        archive = self._post(
            "UVR5",
            f"{self.service_urls.uvr5}/v1/audio/stem-separations",
            files={"audio": (source.filename, audio, source.media_type)},
        )
        expected = {
            "instrumental.wav": "audio/wav",
            "vocal_dry_original.wav": "audio/wav",
        }
        outputs = extract_expected_zip(archive, expected)
        return [
            self.store.create(
                project_id=project,
                operation="stems.separate",
                filename=filename,
                media_type=media_type,
                payload=outputs[filename],
            )
            for filename, media_type in expected.items()
        ]

    def train_voice(
        self,
        *,
        project_id: str,
        audio_asset_id: str,
        model_id: str,
        epochs: int,
        batch_size: int,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        model = require_model_id(model_id)
        if not 10 <= epochs <= 1000:
            raise ValueError("epochs must be between 10 and 1000")
        if not 1 <= batch_size <= 16:
            raise ValueError("batch_size must be between 1 and 16")
        source, audio = self.store.read(audio_asset_id, project)
        archive = self._post(
            "RVC",
            f"{self.service_urls.rvc}/v1/voice-models/train",
            files={"audio": (source.filename, audio, source.media_type)},
            data={
                "model_id": model,
                "epochs": str(epochs),
                "batch_size": str(batch_size),
                "save_every_epochs": "50",
            },
        )
        expected = {
            f"{model}.pth": "application/octet-stream",
            f"{model}.index": "application/octet-stream",
            f"{model}_validation.wav": "audio/wav",
        }
        outputs = extract_expected_zip(archive, expected)
        return [
            self.store.create(
                project_id=project,
                operation="voice.train",
                filename=filename,
                media_type=media_type,
                payload=outputs[filename],
            )
            for filename, media_type in expected.items()
        ]

    def convert_voice(
        self,
        *,
        project_id: str,
        audio_asset_id: str,
        model_id: str,
        pitch_shift: int,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        model = require_model_id(model_id)
        if not -24 <= pitch_shift <= 24:
            raise ValueError("pitch_shift must be between -24 and 24")
        source, audio = self.store.read(audio_asset_id, project)
        converted = self._post(
            "RVC",
            f"{self.service_urls.rvc}/v1/audio/voice-conversions",
            files={"audio": (source.filename, audio, source.media_type)},
            data={"model_id": model, "pitch_shift": str(pitch_shift), "f0_method": "rmvpe"},
        )
        return [
            self.store.create(
                project_id=project,
                operation="voice.convert",
                filename="vocal_dry_cloned.wav",
                media_type="audio/wav",
                payload=converted,
            )
        ]

    def master(
        self,
        *,
        project_id: str,
        instrumental_asset_id: str,
        vocal_asset_id: str,
        lyrics_lrc: str,
        bpm: float,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if not 40.0 <= bpm <= 240.0:
            raise ValueError("bpm must be between 40 and 240")
        instrumental_record, instrumental = self.store.read(instrumental_asset_id, project)
        vocal_record, vocal = self.store.read(vocal_asset_id, project)
        if not lyrics_lrc.strip() or len(lyrics_lrc.encode("utf-8")) > 1024 * 1024:
            raise ValueError("lyrics_lrc must contain 1 byte to 1 MiB of UTF-8 text")
        archive = self._post(
            "Mixer",
            f"{self.service_urls.mixer}/v1/audio/masters",
            files={
                "instrumental": (instrumental_record.filename, instrumental, instrumental_record.media_type),
                "vocal": (vocal_record.filename, vocal, vocal_record.media_type),
                "lyrics_lrc": ("lyrics.lrc", lyrics_lrc.encode("utf-8"), "text/plain; charset=utf-8"),
            },
            data={"bpm": str(bpm)},
        )
        expected = {
            "Final_Song_Master.wav": "audio/wav",
            "Final_Song.mp3": "audio/mpeg",
            "Final_Song.lrc": "text/plain",
        }
        outputs = extract_expected_zip(archive, expected)
        return [
            self.store.create(
                project_id=project,
                operation="mix.master",
                filename=filename,
                media_type=media_type,
                payload=outputs[filename],
            )
            for filename, media_type in expected.items()
        ]
