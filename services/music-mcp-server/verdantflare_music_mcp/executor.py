from __future__ import annotations

import io
import json
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
MAX_VOICE_PREPARATION_BYTES = 300 * 1024 * 1024
VOICE_SEGMENT_PATTERN = re.compile(r"\d{2}-\d{3}\.wav")
LRC_LINE_PATTERN = re.compile(r"^\[(\d{1,3}):(\d{2})\.(\d{3})\](.+)$")


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
    lyrics_aligner: str
    mixer: str

    @classmethod
    def from_environment(cls) -> "ServiceURLs":
        return cls(
            music3=os.environ.get("MUSIC3_URL", "http://music-minimax-music3-api:8000").rstrip("/"),
            uvr5=os.environ.get("UVR5_URL", "http://music-uvr5-api:8000").rstrip("/"),
            rvc=os.environ.get("RVC_URL", "http://music-rvc-api:8000").rstrip("/"),
            lyrics_aligner=os.environ.get(
                "LYRICS_ALIGNER_URL", "http://music-lyrics-aligner-api:8000"
            ).rstrip("/"),
            mixer=os.environ.get("MIXER_URL", "http://music-audio-mixer-api:8000").rstrip("/"),
        )


def validate_pcm_wav(payload: bytes) -> None:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ExecutionError("Music3 returned a compressed WAV")
            if source.getframerate() <= 0 or source.getnframes() <= 0:
                raise ExecutionError("Music3 returned an empty WAV")
    except (EOFError, wave.Error) as error:
        raise ExecutionError("Music3 returned an invalid WAV") from error


def extract_expected_zip(payload: bytes, expected: dict[str, str]) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)) or set(names) != set(expected) | {"manifest.json"}:
                raise ExecutionError("downstream ZIP does not contain the exact expected files")
            if sum(member.file_size for member in members) > 1024 * 1024 * 1024:
                raise ExecutionError("downstream ZIP contents exceed 1 GiB")
            outputs: dict[str, bytes] = {}
            for member in members:
                if member.is_dir() or member.flag_bits & 0x1 or require_filename(member.filename) != member.filename:
                    raise ExecutionError("downstream ZIP contains an unsafe member")
                if member.file_size > 1024 * 1024 * 1024:
                    raise ExecutionError("downstream ZIP member exceeds 1 GiB")
                if member.filename == "manifest.json":
                    continue
                outputs[member.filename] = archive.read(member)
            return outputs
    except (ValueError, zipfile.BadZipFile) as error:
        raise ExecutionError("downstream returned an invalid ZIP") from error


def validate_voice_preparation_outputs(outputs: dict[str, bytes], source_count: int) -> None:
    wav_names = [f"prepared-{number:02d}.wav" for number in range(1, source_count + 1)]
    wav_names.append("voice-training.wav")
    for name in wav_names:
        try:
            with wave.open(io.BytesIO(outputs[name]), "rb") as source:
                if (
                    source.getcomptype() != "NONE"
                    or source.getnchannels() != 1
                    or source.getsampwidth() != 2
                    or source.getframerate() != 40000
                    or source.getnframes() <= 0
                ):
                    raise ExecutionError("RVC returned an invalid prepared WAV")
        except (EOFError, wave.Error) as error:
            raise ExecutionError("RVC returned an invalid prepared WAV") from error
    try:
        report = json.loads(outputs["voice-preparation-report.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutionError("RVC returned an invalid voice preparation report") from error
    if not isinstance(report, dict) or not isinstance(report.get("sources"), list):
        raise ExecutionError("RVC returned an inconsistent voice preparation report")
    if (
        report.get("schema_version") != 1
        or report.get("automatic_status") != "passed"
        or report.get("review_required") is not True
        or len(report["sources"]) != source_count
    ):
        raise ExecutionError("RVC returned an inconsistent voice preparation report")
    try:
        with zipfile.ZipFile(io.BytesIO(outputs["voice-segments.zip"])) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            segment_names = [name for name in names if name != "manifest.json"]
            if (
                len(names) != len(set(names))
                or names.count("manifest.json") != 1
                or not segment_names
                or any(not VOICE_SEGMENT_PATTERN.fullmatch(name) for name in segment_names)
                or any(member.is_dir() or member.flag_bits & 0x1 for member in members)
                or sum(member.file_size for member in members) > 1024 * 1024 * 1024
            ):
                raise ExecutionError("RVC returned an unsafe voice segments ZIP")
            source_numbers = {int(name[:2]) for name in segment_names}
            if source_numbers != set(range(1, source_count + 1)):
                raise ExecutionError("RVC returned incomplete voice segments")
    except zipfile.BadZipFile as error:
        raise ExecutionError("RVC returned an invalid voice segments ZIP") from error


def validate_aligned_lrc(payload: bytes, lyrics: str) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExecutionError("lyrics aligner returned non-UTF-8 text") from error
    source_lines = [line.strip() for line in lyrics.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    source_lines = [line for line in source_lines if line]
    output_lines = [line for line in text.splitlines() if line]
    if len(output_lines) != len(source_lines):
        raise ExecutionError("lyrics aligner changed the lyric line count")

    timestamps: list[int] = []
    output_lyrics: list[str] = []
    for line in output_lines:
        match = LRC_LINE_PATTERN.fullmatch(line)
        if match is None:
            raise ExecutionError("lyrics aligner returned invalid LRC")
        minutes, seconds, milliseconds, lyric = match.groups()
        if int(seconds) >= 60:
            raise ExecutionError("lyrics aligner returned invalid LRC seconds")
        timestamps.append((int(minutes) * 60 + int(seconds)) * 1000 + int(milliseconds))
        output_lyrics.append(lyric)
    if output_lyrics != source_lines:
        raise ExecutionError("lyrics aligner changed the approved lyrics")
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise ExecutionError("lyrics aligner returned non-increasing timestamps")
    return "\n".join(output_lines) + "\n"


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
        max_duration_seconds: float,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if not lyrics.strip() or not instructions.strip():
            raise ValueError("lyrics and instructions are required")
        if not 1 <= candidate_number <= 99:
            raise ValueError("candidate_number must be between 1 and 99")
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed must be between 0 and 2147483647")
        if not 1.0 <= max_duration_seconds <= 300.0:
            raise ValueError("max_duration_seconds must be between 1 and 300")

        max_new_tokens = math.ceil(max_duration_seconds * 25)
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
        validate_pcm_wav(raw)
        return [
            self.store.create(
                project_id=project,
                operation="music.generate",
                filename=f"Demo_Candidate_{candidate_number}.wav",
                media_type="audio/wav",
                payload=raw,
            )
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

    def prepare_voice(
        self,
        *,
        project_id: str,
        audio_asset_ids: list[str],
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if not 1 <= len(audio_asset_ids) <= 20:
            raise ValueError("audio_asset_ids must contain 1-20 artifacts")
        if len(audio_asset_ids) != len(set(audio_asset_ids)):
            raise ValueError("audio_asset_ids must not contain duplicates")
        sources: list[tuple[ArtifactRecord, bytes]] = []
        total_size = 0
        for artifact_id in audio_asset_ids:
            record, payload = self.store.read(artifact_id, project)
            if not record.media_type.startswith("audio/"):
                raise ValueError("audio_asset_ids must reference audio artifacts")
            total_size += len(payload)
            if total_size > MAX_VOICE_PREPARATION_BYTES:
                raise ValueError("voice preparation inputs exceed 300 MiB")
            sources.append((record, payload))
        archive = self._post(
            "RVC",
            f"{self.service_urls.rvc}/v1/voice-datasets/prepare",
            files=[
                ("audio", (record.filename, payload, record.media_type))
                for record, payload in sources
            ],
        )
        expected = {
            **{f"prepared-{number:02d}.wav": "audio/wav" for number in range(1, len(sources) + 1)},
            "voice-training.wav": "audio/wav",
            "voice-segments.zip": "application/zip",
            "voice-preparation-report.json": "application/json",
        }
        outputs = extract_expected_zip(archive, expected)
        validate_voice_preparation_outputs(outputs, len(sources))
        return [
            self.store.create(
                project_id=project,
                operation="voice.prepare",
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
        f0_method: str = "rmvpe",
        index_rate: float = 0.66,
        filter_radius: int = 3,
        rms_mix_rate: float = 1.0,
        protect: float = 0.33,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        model = require_model_id(model_id)
        if not -24 <= pitch_shift <= 24:
            raise ValueError("pitch_shift must be between -24 and 24")
        if f0_method not in {"rmvpe", "harvest", "pm", "crepe"}:
            raise ValueError("f0_method must be rmvpe, harvest, pm, or crepe")
        if not 0.0 <= index_rate <= 1.0:
            raise ValueError("index_rate must be between 0 and 1")
        if not 0 <= filter_radius <= 7:
            raise ValueError("filter_radius must be between 0 and 7")
        if not 0.0 <= rms_mix_rate <= 1.0:
            raise ValueError("rms_mix_rate must be between 0 and 1")
        if not 0.0 <= protect <= 0.5:
            raise ValueError("protect must be between 0 and 0.5")
        source, audio = self.store.read(audio_asset_id, project)
        converted = self._post(
            "RVC",
            f"{self.service_urls.rvc}/v1/audio/voice-conversions",
            files={"audio": (source.filename, audio, source.media_type)},
            data={
                "model_id": model,
                "pitch_shift": str(pitch_shift),
                "f0_method": f0_method,
                "index_rate": str(index_rate),
                "filter_radius": str(filter_radius),
                "rms_mix_rate": str(rms_mix_rate),
                "protect": str(protect),
            },
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

    def align_lyrics(
        self,
        *,
        project_id: str,
        vocal_asset_id: str,
        lyrics: str,
        language: str,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if language != "zh":
            raise ValueError("language must be zh")
        if not lyrics.strip() or len(lyrics.encode("utf-8")) > 1024 * 1024:
            raise ValueError("lyrics must contain 1 byte to 1 MiB of UTF-8 text")
        vocal_record, vocal = self.store.read(vocal_asset_id, project)
        aligned = self._post(
            "Lyrics aligner",
            f"{self.service_urls.lyrics_aligner}/v1/lyrics/alignments",
            files={
                "audio": (vocal_record.filename, vocal, vocal_record.media_type),
                "lyrics": ("lyrics.txt", lyrics.encode("utf-8"), "text/plain; charset=utf-8"),
            },
            data={"language": language},
        )
        lrc = validate_aligned_lrc(aligned, lyrics)
        return [
            self.store.create(
                project_id=project,
                operation="lyrics.align",
                filename="Aligned_Lyrics.lrc",
                media_type="text/plain",
                payload=lrc.encode("utf-8"),
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
