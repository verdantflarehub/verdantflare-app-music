from __future__ import annotations

import audioop
import hashlib
import io
import json
import math
import os
import re
import time
import wave
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactRecord,
    ArtifactStore,
    require_filename,
    require_project_id,
)
from .duet import DuetLine, build_duet_plan, fit_vocal_to_backing, lyrics_for_voice, pcm16_wav, preview_duet, read_duet_plan


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
    music_editor: str | None = None
    music_editor_backend: str = "ace_step_v1_5"
    music_editor_bearer_token: str | None = None
    duet_model: str = "acestep-v15-base"

    @classmethod
    def from_environment(cls) -> "ServiceURLs":
        editor = os.environ.get("MUSIC_EDITOR_URL", "").strip().rstrip("/")
        return cls(
            music3=os.environ.get("MUSIC3_URL", "http://music-minimax-music3-api:8000").rstrip("/"),
            uvr5=os.environ.get("UVR5_URL", "http://music-uvr5-api:8000").rstrip("/"),
            rvc=os.environ.get("RVC_URL", "http://music-rvc-api:8000").rstrip("/"),
            lyrics_aligner=os.environ.get(
                "LYRICS_ALIGNER_URL", "http://music-lyrics-aligner-api:8000"
            ).rstrip("/"),
            mixer=os.environ.get("MIXER_URL", "http://music-audio-mixer-api:8000").rstrip("/"),
            music_editor=editor or None,
            music_editor_backend=os.environ.get(
                "MUSIC_EDITOR_BACKEND", "ace_step_v1_5"
            ).strip(),
            music_editor_bearer_token=os.environ.get("MUSIC_EDITOR_BEARER_TOKEN") or None,
            duet_model=os.environ.get("MUSIC_DUET_MODEL", "acestep-v15-base").strip(),
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


def preserve_pcm_outside_region(
    source_payload: bytes,
    edited_payload: bytes,
    start_seconds: float,
    end_seconds: float,
    crossfade_seconds: float,
) -> bytes:
    """Insert a conditioned repaint while keeping every source frame outside the mask unchanged."""
    try:
        with wave.open(io.BytesIO(source_payload), "rb") as source:
            source_params = source.getparams()
            source_frames = source.readframes(source.getnframes())
        with wave.open(io.BytesIO(edited_payload), "rb") as edited:
            edited_params = edited.getparams()
            edited_frames = edited.readframes(edited.getnframes())
    except (EOFError, wave.Error) as error:
        raise ExecutionError("music redraw requires valid PCM WAV audio") from error
    if source_params.comptype != "NONE" or edited_params.comptype != "NONE":
        raise ExecutionError("music redraw requires uncompressed PCM WAV audio")
    if source_params.sampwidth != 2:
        raise ExecutionError("music redraw source must be 16-bit PCM WAV")
    if source_params.nchannels not in {1, 2} or edited_params.nchannels not in {1, 2}:
        raise ExecutionError("music redraw supports mono or stereo WAV audio")

    if edited_params.sampwidth != source_params.sampwidth:
        edited_frames = audioop.lin2lin(
            edited_frames, edited_params.sampwidth, source_params.sampwidth
        )
    if edited_params.nchannels != source_params.nchannels:
        if edited_params.nchannels == 1:
            edited_frames = audioop.tostereo(edited_frames, source_params.sampwidth, 1.0, 1.0)
        else:
            edited_frames = audioop.tomono(edited_frames, source_params.sampwidth, 0.5, 0.5)
    if edited_params.framerate != source_params.framerate:
        edited_frames, _ = audioop.ratecv(
            edited_frames,
            source_params.sampwidth,
            source_params.nchannels,
            edited_params.framerate,
            source_params.framerate,
            None,
        )

    frame_bytes = source_params.sampwidth * source_params.nchannels
    source_frame_count = len(source_frames) // frame_bytes
    edited_frame_count = len(edited_frames) // frame_bytes
    start_frame = round(start_seconds * source_params.framerate)
    end_frame = round(end_seconds * source_params.framerate)
    if end_frame > source_frame_count or end_frame > edited_frame_count:
        raise ExecutionError("music redraw result is shorter than the requested region")

    start_byte = start_frame * frame_bytes
    end_byte = end_frame * frame_bytes
    region = bytearray(edited_frames[start_byte:end_byte])
    source_region = source_frames[start_byte:end_byte]
    fade_frames = min(
        round(crossfade_seconds * source_params.framerate),
        (end_frame - start_frame) // 2,
    )
    for frame_index in range(fade_frames):
        left_edit_weight = (frame_index + 1) / fade_frames
        right_edit_weight = (fade_frames - frame_index - 1) / fade_frames
        for channel in range(source_params.nchannels):
            for relative_frame, edit_weight in (
                (frame_index, left_edit_weight),
                (end_frame - start_frame - fade_frames + frame_index, right_edit_weight),
            ):
                offset = relative_frame * frame_bytes + channel * 2
                source_sample = int.from_bytes(
                    source_region[offset : offset + 2], "little", signed=True
                )
                edited_sample = int.from_bytes(region[offset : offset + 2], "little", signed=True)
                mixed = round(source_sample * (1.0 - edit_weight) + edited_sample * edit_weight)
                region[offset : offset + 2] = max(-32768, min(32767, mixed)).to_bytes(
                    2, "little", signed=True
                )

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setparams(source_params)
        target.writeframes(source_frames[:start_byte] + region + source_frames[end_byte:])
    return output.getvalue()


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

    def _get_json(self, service: str, url: str, **kwargs: object) -> dict[str, object]:
        try:
            response = self.client.get(url, **kwargs)
        except httpx.HTTPError as error:
            raise ExecutionError(f"{service} request failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ExecutionError(f"{service} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise ExecutionError(f"{service} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ExecutionError(f"{service} returned invalid JSON")
        return payload

    def _post_json(self, service: str, url: str, **kwargs: object) -> dict[str, object]:
        try:
            response = self.client.post(url, **kwargs)
        except httpx.HTTPError as error:
            raise ExecutionError(f"{service} request failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ExecutionError(f"{service} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise ExecutionError(f"{service} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ExecutionError(f"{service} returned invalid JSON")
        return payload

    def _music_editor_headers(self) -> dict[str, str]:
        token = self.service_urls.music_editor_bearer_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _ace_data(payload: dict[str, object], operation: str) -> object:
        if payload.get("code") != 200 or payload.get("error") not in {None, ""}:
            raise ExecutionError(f"ACE-Step {operation} failed")
        if "data" not in payload:
            raise ExecutionError(f"ACE-Step {operation} returned invalid JSON")
        return payload["data"]

    def preflight(
        self,
        *,
        workflow: str,
        voice_source: str | None = None,
        voice_model_id: str | None = None,
        require_local_redraw: bool = False,
    ) -> dict[str, object]:
        base_requirements = {
            "generate": ("music3",),
            "full_song": ("music3", "lyrics_aligner", "mixer"),
            "cover": ("uvr5", "rvc", "lyrics_aligner", "mixer"),
            "voice_training": ("rvc",),
            "redraw": ("music_editor",),
            "duet_generate": ("music_editor",),
        }
        if workflow not in base_requirements:
            raise ValueError("workflow must be generate, full_song, cover, voice_training, redraw, or duet_generate")

        voice_sources = {"generated_voice", "approved_model", "authorized_recordings"}
        if voice_source is not None and voice_source not in voice_sources:
            raise ValueError(
                "voice_source must be generated_voice, approved_model, or authorized_recordings"
            )
        if workflow in {"full_song", "cover"} and voice_source is None:
            raise ValueError("voice_source is required for full_song and cover preflight")
        if workflow == "generate" and voice_source not in {None, "generated_voice"}:
            raise ValueError("generate preflight only supports generated_voice")
        if workflow == "cover" and voice_source == "generated_voice":
            raise ValueError("cover preflight cannot use generated_voice")
        if workflow == "voice_training" and voice_source != "authorized_recordings":
            raise ValueError("voice_training preflight requires authorized_recordings")
        if workflow in {"redraw", "duet_generate"} and voice_source is not None:
            raise ValueError(f"{workflow} preflight does not accept voice_source")

        model_id = require_model_id(voice_model_id) if voice_model_id else None
        if voice_source == "approved_model" and model_id is None:
            raise ValueError("voice_model_id is required when voice_source is approved_model")
        if voice_source != "approved_model" and model_id is not None:
            raise ValueError("voice_model_id is only valid when voice_source is approved_model")

        service_urls = {
            "music3": self.service_urls.music3,
            "uvr5": self.service_urls.uvr5,
            "rvc": self.service_urls.rvc,
            "lyrics_aligner": self.service_urls.lyrics_aligner,
            "mixer": self.service_urls.mixer,
            "music_editor": self.service_urls.music_editor,
        }
        required_services = set(base_requirements[workflow])
        if workflow == "full_song" and voice_source in {"approved_model", "authorized_recordings"}:
            required_services.update({"uvr5", "rvc"})
        if require_local_redraw:
            required_services.add("music_editor")
        checks: list[dict[str, object]] = []
        blocking_conditions: list[str] = []

        for service in sorted(required_services):
            url = service_urls[service]
            if url is None:
                checks.append({"service": service, "ready": False, "reason": "not_configured"})
                blocking_conditions.append(f"{service} is not configured")
                continue
            try:
                kwargs = (
                    {"headers": self._music_editor_headers()}
                    if service == "music_editor"
                    else {}
                )
                payload = self._get_json(service, f"{url}/health", **kwargs)
                ready = payload.get("status") not in {"unavailable", "error", "failed"}
                checks.append(
                    {
                        "service": service,
                        "ready": ready,
                        "reason": "ok" if ready else "health_not_ready",
                    }
                )
                if not ready:
                    blocking_conditions.append(f"{service} health check is not ready")
            except ExecutionError:
                checks.append({"service": service, "ready": False, "reason": "health_request_failed"})
                blocking_conditions.append(f"{service} health check failed")

        voice_model: dict[str, object] | None = None
        if model_id is not None:
            try:
                payload = self._get_json("RVC model catalog", f"{self.service_urls.rvc}/v1/voice-models")
                models = payload.get("models")
                installed = {
                    item.get("id")
                    for item in models
                    if isinstance(models, list) and isinstance(item, dict)
                } if isinstance(models, list) else set()
                found = model_id in installed
                voice_model = {"model_id": model_id, "installed": found}
                if not found:
                    blocking_conditions.append(f"voice model {model_id} is not installed")
            except ExecutionError:
                voice_model = {"model_id": model_id, "installed": False, "reason": "catalog_request_failed"}
                blocking_conditions.append("RVC model catalog check failed")

        redraw_configured = self.service_urls.music_editor is not None
        redraw_ready = any(
            check["service"] == "music_editor" and check["ready"] is True for check in checks
        )
        duet_model_ready = False
        if workflow == "duet_generate":
            if self.service_urls.music_editor_backend != "ace_step_v1_5":
                blocking_conditions.append("duet generation requires the ACE-Step 1.5 backend")
            if self.service_urls.duet_model not in {"acestep-v15-base", "acestep-v15-xl-base"}:
                blocking_conditions.append("duet generation requires an ACE-Step 1.5 base model")
            if redraw_ready and not blocking_conditions:
                try:
                    catalog = self._get_json(
                        "ACE-Step model catalog",
                        f"{self.service_urls.music_editor}/v1/model_inventory",
                        headers=self._music_editor_headers(),
                    )
                    data = self._ace_data(catalog, "model catalog")
                    models = data.get("models") if isinstance(data, dict) else None
                    duet_model_ready = isinstance(models, list) and any(
                        isinstance(item, dict)
                        and item.get("name") == self.service_urls.duet_model
                        and item.get("is_loaded") is True
                        for item in models
                    )
                except ExecutionError:
                    pass
                if not duet_model_ready:
                    blocking_conditions.append("ACE-Step duet base model is not loaded")
        return {
            "status": "ready" if not blocking_conditions else "blocked",
            "workflow": workflow,
            "voice_source": voice_source,
            "checks": checks,
            "voice_model": voice_model,
            "local_redraw": {
                "configured": redraw_configured,
                "ready": redraw_ready,
                "backend": self.service_urls.music_editor_backend,
                "contract": "ace_step_v1_5_repaint",
            },
            "duet_generation": {
                "model": self.service_urls.duet_model,
                "ready": duet_model_ready,
                "contract": "ace_step_v1_5_text2music_lego",
            },
            "blocking_conditions": blocking_conditions,
            "guidance": (
                (
                    "Prepare and train the authorized recordings, then rerun preflight with "
                    "voice_source=approved_model and the returned exact model ID before conversion."
                    if voice_source == "authorized_recordings"
                    else "Proceed with the selected workflow."
                )
                if not blocking_conditions
                else "Resolve the listed conditions before starting dependent paid or irreversible stages."
            ),
        }

    def duet_plan(
        self, *, project_id: str, lines: list[DuetLine], bpm: float, style: str,
        candidate_number: int,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        plan = build_duet_plan(lines, bpm=bpm, style=style, candidate_number=candidate_number)
        return [self.store.create(
            project_id=project, operation="duet.plan",
            filename=f"Duet_Candidate_{candidate_number}_Plan.json",
            media_type="application/json",
            payload=(json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )]

    def _read_duet_plan(self, project: str, plan_asset_id: str) -> dict[str, object]:
        record, payload = self.store.read(plan_asset_id, project)
        if record.operation != "duet.plan":
            raise ValueError("plan_asset_id must refer to a duet plan")
        return read_duet_plan(payload)

    def _duet_backend_ready(self) -> None:
        if self.service_urls.music_editor is None:
            raise ExecutionError("ACE-Step duet backend is not configured")
        if self.service_urls.music_editor_backend != "ace_step_v1_5":
            raise ExecutionError("duet generation requires the ACE-Step 1.5 backend")
        if self.service_urls.duet_model not in {"acestep-v15-base", "acestep-v15-xl-base"}:
            raise ExecutionError("duet generation requires an ACE-Step 1.5 base model")

    def _ace_duet_audio(
        self, *, project: str, plan_asset_id: str, data: dict[str, str],
        source: tuple[str, bytes] | None = None, resume_task_asset_id: str | None = None,
    ) -> tuple[bytes, ArtifactRecord]:
        self._duet_backend_ready()
        request_hash = hashlib.sha256(json.dumps({
            "plan_asset_id": plan_asset_id, "data": data,
            "source_sha256": hashlib.sha256(source[1]).hexdigest() if source else None,
        }, sort_keys=True).encode()).hexdigest()
        if resume_task_asset_id is not None:
            receipt_record, receipt_payload = self.store.read(resume_task_asset_id, project)
            if receipt_record.operation != "duet.task":
                raise ValueError("resume_task_asset_id must refer to a duet task")
            try:
                receipt = json.loads(receipt_payload)
            except (ValueError, TypeError) as error:
                raise ValueError("duet task receipt is invalid") from error
            if receipt.get("request_sha256") != request_hash or not isinstance(receipt.get("task_id"), str):
                raise ValueError("duet task receipt does not match this request")
            task_id = receipt["task_id"]
        else:
            kwargs: dict[str, object] = {"headers": self._music_editor_headers(), "data": data}
            if source is not None:
                kwargs["files"] = {"src_audio": (source[0], source[1], "audio/wav")}
            submitted = self._post_json(
                "ACE-Step duet task submission",
                f"{self.service_urls.music_editor}/release_task",
                **kwargs,
            )
            submission_data = self._ace_data(submitted, "duet task submission")
            if not isinstance(submission_data, dict) or not isinstance(submission_data.get("task_id"), str):
                raise ExecutionError("ACE-Step duet task submission returned no task ID")
            task_id = submission_data["task_id"]
            try:
                receipt_record = self.store.create(
                    project_id=project, operation="duet.task",
                    filename="Duet_Task.json",
                    media_type="application/json",
                    payload=json.dumps({"task_id": task_id, "request_sha256": request_hash}).encode(),
                )
            except Exception as error:
                raise ExecutionError(f"ACE-Step duet task {task_id} submitted but receipt could not be saved") from error
        recovery = f"; resume with Artifact {receipt_record.artifact_id}"
        deadline = time.monotonic() + 900.0
        while time.monotonic() < deadline:
            try:
                queried = self._post_json(
                    "ACE-Step duet task query",
                    f"{self.service_urls.music_editor}/query_result",
                    headers=self._music_editor_headers(),
                    json={"task_id_list": [task_id]},
                )
            except ExecutionError as error:
                raise ExecutionError(f"ACE-Step duet task {task_id} query failed{recovery}") from error
            try:
                query_data = self._ace_data(queried, "duet task query")
            except ExecutionError as error:
                raise ExecutionError(f"ACE-Step duet task {task_id} query returned an error{recovery}") from error
            if not isinstance(query_data, list) or len(query_data) != 1:
                raise ExecutionError(f"ACE-Step duet task {task_id} returned invalid status{recovery}")
            task = query_data[0]
            if not isinstance(task, dict) or task.get("task_id") != task_id:
                raise ExecutionError(f"ACE-Step duet task {task_id} returned the wrong task{recovery}")
            if task.get("status") == 2:
                raise ExecutionError(f"ACE-Step duet task {task_id} failed{recovery}")
            if task.get("status") == 1:
                try:
                    items = json.loads(task["result"]) if isinstance(task.get("result"), str) else task["result"]
                except (KeyError, json.JSONDecodeError) as error:
                    raise ExecutionError(f"ACE-Step duet task {task_id} returned invalid output{recovery}") from error
                break
            time.sleep(2.0)
        else:
            raise ExecutionError(f"ACE-Step duet task {task_id} timed out{recovery}")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ExecutionError(f"ACE-Step duet task {task_id} returned no audio{recovery}")
        file_value = items[0].get("file")
        if not isinstance(file_value, str) or not file_value:
            raise ExecutionError(f"ACE-Step duet task {task_id} returned no audio URL{recovery}")
        result_url = urljoin(f"{self.service_urls.music_editor}/", file_value)
        origin = urlsplit(self.service_urls.music_editor)
        result_origin = urlsplit(result_url)
        if (origin.scheme, origin.netloc) != (result_origin.scheme, result_origin.netloc):
            raise ExecutionError(f"ACE-Step duet task {task_id} returned an untrusted origin{recovery}")
        try:
            response = self.client.get(result_url, headers=self._music_editor_headers())
        except httpx.HTTPError as error:
            raise ExecutionError(f"ACE-Step duet task {task_id} audio download failed{recovery}") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ExecutionError(f"ACE-Step duet task {task_id} audio download returned HTTP {response.status_code}{recovery}")
        if not response.content or len(response.content) > MAX_ARTIFACT_BYTES:
            raise ExecutionError(f"ACE-Step duet task {task_id} audio download has invalid size{recovery}")
        try:
            pcm16_wav(response.content)
        except ValueError as error:
            raise ExecutionError(f"ACE-Step duet task {task_id} did not return 16-bit PCM WAV{recovery}") from error
        return response.content, receipt_record

    def duet_instrumental(
        self, *, project_id: str, plan_asset_id: str, seed: int = 7,
        resume_task_asset_id: str | None = None,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        plan = self._read_duet_plan(project, plan_asset_id)
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed must be between 0 and 2147483647")
        self._duet_backend_ready()
        data = {
            "task_type": "text2music", "model": self.service_urls.duet_model,
            "prompt": f"Instrumental only, no vocals or spoken voice. {plan['style']}",
            "lyrics": "[Instrumental]", "audio_duration": str(plan["duration_seconds"]),
            "bpm": str(plan["bpm"]), "seed": str(seed),
            "use_random_seed": "false", "audio_format": "wav",
            "inference_steps": "50", "batch_size": "1", "thinking": "false",
        }
        audio, receipt = self._ace_duet_audio(
            project=project, plan_asset_id=plan_asset_id, data=data,
            resume_task_asset_id=resume_task_asset_id,
        )
        params, _ = pcm16_wav(audio)
        if abs(params.nframes / params.framerate - plan["duration_seconds"]) > max(2, plan["duration_seconds"] * 0.03):
            rejected = self.store.create(
                project_id=project, operation="duet.instrumental.rejected",
                filename=f"Duet_Candidate_{plan['candidate_number']}_Instrumental_Rejected.wav",
                media_type="audio/wav", payload=audio,
            )
            raise ExecutionError(f"ACE-Step duet instrumental duration differs from the approved plan; diagnostic Artifact {rejected.artifact_id}")
        record = self.store.create(
            project_id=project, operation="duet.instrumental",
            filename=f"Duet_Candidate_{plan['candidate_number']}_Instrumental.wav",
            media_type="audio/wav", payload=audio,
        )
        return [receipt, record]

    def duet_vocal(
        self, *, project_id: str, plan_asset_id: str, instrumental_asset_id: str,
        voice: str, seed: int = 7, resume_task_asset_id: str | None = None,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        plan = self._read_duet_plan(project, plan_asset_id)
        if voice not in {"female", "male"}:
            raise ValueError("voice must be female or male")
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed must be between 0 and 2147483647")
        backing_record, backing = self.store.read(instrumental_asset_id, project)
        if backing_record.operation != "duet.instrumental" or backing_record.filename != f"Duet_Candidate_{plan['candidate_number']}_Instrumental.wav":
            raise ValueError("instrumental_asset_id does not match the duet plan")
        self._duet_backend_ready()
        description = "bright natural female lead" if voice == "female" else "clear lower male lead"
        data = {
            "task_type": "lego", "model": self.service_urls.duet_model,
            "instruction": "Generate only the isolated vocals track based on the audio context:",
            "prompt": f"{description} singing in Mandarin. No instruments. {plan['style']}",
            "lyrics": lyrics_for_voice(plan, voice),
            "vocal_language": "zh", "audio_duration": str(plan["duration_seconds"]),
            "bpm": str(plan["bpm"]), "seed": str(seed),
            "use_random_seed": "false", "audio_format": "wav",
            "inference_steps": "50", "batch_size": "1", "thinking": "false",
            "track_name": "Vocals",
            "repainting_start": "0", "repainting_end": "-1",
        }
        raw, receipt = self._ace_duet_audio(
            project=project, plan_asset_id=plan_asset_id, data=data,
            source=(backing_record.filename, backing),
            resume_task_asset_id=resume_task_asset_id,
        )
        raw_record = self.store.create(
            project_id=project, operation="duet.vocal.raw",
            filename=f"Duet_Candidate_{plan['candidate_number']}_{voice.title()}_Raw.wav",
            media_type="audio/wav", payload=raw,
        )
        try:
            masked = fit_vocal_to_backing(backing, raw, plan, voice)
        except ValueError as error:
            raise ExecutionError(f"duet vocal validation failed; raw Artifact {raw_record.artifact_id}: {error}") from error
        record = self.store.create(
            project_id=project, operation="duet.vocal",
            filename=f"Duet_Candidate_{plan['candidate_number']}_{voice.title()}.wav",
            media_type="audio/wav", payload=masked,
        )
        return [receipt, raw_record, record]

    def duet_preview(
        self, *, project_id: str, plan_asset_id: str, instrumental_asset_id: str,
        female_asset_id: str, male_asset_id: str,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        plan = self._read_duet_plan(project, plan_asset_id)
        prefix = f"Duet_Candidate_{plan['candidate_number']}"
        inputs = [
            (instrumental_asset_id, "duet.instrumental", f"{prefix}_Instrumental.wav"),
            (female_asset_id, "duet.vocal", f"{prefix}_Female.wav"),
            (male_asset_id, "duet.vocal", f"{prefix}_Male.wav"),
        ]
        if len({item[0] for item in inputs}) != 3:
            raise ValueError("duet preview requires three distinct artifacts")
        audio_parts = []
        for artifact_id, operation, filename in inputs:
            record, payload = self.store.read(artifact_id, project)
            if record.operation != operation or record.filename != filename:
                raise ValueError("duet preview input does not match the plan or vocal role")
            audio_parts.append(payload)
        preview = preview_duet(*audio_parts)
        return [self.store.create(
            project_id=project, operation="duet.preview",
            filename=f"{prefix}.wav", media_type="audio/wav", payload=preview,
        )]

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

    def redraw(
        self,
        *,
        project_id: str,
        audio_asset_id: str,
        start_seconds: float,
        end_seconds: float,
        instructions: str,
        lyrics: str,
        revision_number: int,
        seed: int,
        crossfade_seconds: float,
        preservation_mode: str = "balanced",
        edit_strength: float = 0.5,
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if self.service_urls.music_editor is None:
            raise ExecutionError("local music redraw is not configured")
        if self.service_urls.music_editor_backend != "ace_step_v1_5":
            raise ExecutionError("unsupported local music redraw backend")
        if not 0.0 <= start_seconds < end_seconds:
            raise ValueError("redraw range must satisfy 0 <= start_seconds < end_seconds")
        if end_seconds - start_seconds > 60.0:
            raise ValueError("redraw range must not exceed 60 seconds")
        if not instructions.strip():
            raise ValueError("instructions are required")
        if not 1 <= revision_number <= 99:
            raise ValueError("revision_number must be between 1 and 99")
        if not 0 <= seed <= 2_147_483_647:
            raise ValueError("seed must be between 0 and 2147483647")
        if not 0.05 <= crossfade_seconds <= 3.0:
            raise ValueError("crossfade_seconds must be between 0.05 and 3.0")
        if crossfade_seconds * 2 > end_seconds - start_seconds:
            raise ValueError("crossfade_seconds is too long for the redraw range")
        if preservation_mode not in {"conservative", "balanced", "aggressive"}:
            raise ValueError(
                "preservation_mode must be conservative, balanced, or aggressive"
            )
        if not 0.0 <= edit_strength <= 1.0:
            raise ValueError("edit_strength must be between 0.0 and 1.0")

        source, audio = self.store.read(audio_asset_id, project)
        try:
            with wave.open(io.BytesIO(audio), "rb") as source_audio:
                if (
                    source_audio.getcomptype() != "NONE"
                    or source_audio.getsampwidth() != 2
                    or source_audio.getnchannels() not in {1, 2}
                ):
                    raise ExecutionError(
                        "music redraw source must be mono or stereo 16-bit PCM WAV"
                    )
                source_duration = source_audio.getnframes() / source_audio.getframerate()
        except (EOFError, wave.Error) as error:
            raise ExecutionError("music redraw source must be a valid PCM WAV") from error
        if end_seconds > source_duration:
            raise ValueError("redraw range exceeds the source audio duration")
        headers = self._music_editor_headers()
        submitted = self._post_json(
            "ACE-Step task submission",
            f"{self.service_urls.music_editor}/release_task",
            headers=headers,
            files={"src_audio": (source.filename, audio, source.media_type)},
            data={
                "task_type": "repaint",
                "prompt": instructions,
                "lyrics": lyrics,
                "repainting_start": str(start_seconds),
                "repainting_end": str(end_seconds),
                "chunk_mask_mode": "explicit",
                "repaint_mode": preservation_mode,
                "repaint_strength": str(edit_strength),
                "repaint_latent_crossfade_frames": str(max(1, round(crossfade_seconds * 25))),
                "repaint_wav_crossfade_sec": str(crossfade_seconds),
                "seed": str(seed),
                "use_random_seed": "false",
                "audio_format": "wav",
            },
        )
        submission_data = self._ace_data(submitted, "task submission")
        if not isinstance(submission_data, dict) or not isinstance(
            submission_data.get("task_id"), str
        ):
            raise ExecutionError("ACE-Step task submission returned no task ID")
        task_id = submission_data["task_id"]

        result_items: list[object] | None = None
        deadline = time.monotonic() + 900.0
        while time.monotonic() < deadline:
            queried = self._post_json(
                "ACE-Step task query",
                f"{self.service_urls.music_editor}/query_result",
                headers=headers,
                json={"task_id_list": [task_id]},
            )
            query_data = self._ace_data(queried, "task query")
            if not isinstance(query_data, list) or len(query_data) != 1:
                raise ExecutionError("ACE-Step task query returned invalid data")
            task = query_data[0]
            if not isinstance(task, dict) or task.get("task_id") != task_id:
                raise ExecutionError("ACE-Step task query returned the wrong task")
            if task.get("status") == 2:
                raise ExecutionError("ACE-Step repaint task failed")
            if task.get("status") == 1:
                raw_result = task.get("result")
                try:
                    result_items = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
                except json.JSONDecodeError as error:
                    raise ExecutionError("ACE-Step repaint result is invalid") from error
                break
            time.sleep(2.0)
        if result_items is None:
            raise ExecutionError("ACE-Step repaint task timed out")
        if not isinstance(result_items, list) or not result_items or not isinstance(result_items[0], dict):
            raise ExecutionError("ACE-Step repaint result is empty")
        file_value = result_items[0].get("file")
        if not isinstance(file_value, str) or not file_value:
            raise ExecutionError("ACE-Step repaint result has no audio file")

        result_url = urljoin(f"{self.service_urls.music_editor}/", file_value)
        editor_origin = urlsplit(self.service_urls.music_editor)
        result_origin = urlsplit(result_url)
        if (result_origin.scheme, result_origin.netloc) != (
            editor_origin.scheme,
            editor_origin.netloc,
        ):
            raise ExecutionError("ACE-Step repaint result points to an untrusted origin")
        try:
            response = self.client.get(result_url, headers=headers)
        except httpx.HTTPError as error:
            raise ExecutionError("ACE-Step audio download failed") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ExecutionError(f"ACE-Step audio download returned HTTP {response.status_code}")
        if not response.content or len(response.content) > MAX_ARTIFACT_BYTES:
            raise ExecutionError("ACE-Step audio download has an invalid size")
        redrawn = preserve_pcm_outside_region(
            audio,
            response.content,
            start_seconds,
            end_seconds,
            crossfade_seconds,
        )
        validate_pcm_wav(redrawn)
        return [
            self.store.create(
                project_id=project,
                operation="music.redraw",
                filename=f"Demo_Redraw_{revision_number}.wav",
                media_type="audio/wav",
                payload=redrawn,
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
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as stem_archive:
                names = set(stem_archive.namelist())
        except zipfile.BadZipFile as error:
            raise ExecutionError("downstream returned an invalid ZIP") from error
        preserved = {
            "vocal_wet_original.wav": "audio/wav",
            "vocal_reverb_original.wav": "audio/wav",
        }
        backing = {
            "vocal_lead_reference.wav": "audio/wav",
            "backing_vocals_unreviewed.wav": "audio/wav",
        }
        if names == set(expected) | set(preserved) | set(backing) | {"manifest.json"}:
            expected.update(preserved)
            expected.update(backing)
        elif names == set(expected) | set(preserved) | {"manifest.json"}:
            expected.update(preserved)
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
        backing_vocal_asset_id: str | None = None,
        backing_gain_db: float = -6.0,
        additional_vocal_asset_ids: list[str] | None = None,
        additional_vocal_gains_db: list[float] | None = None,
        vocal_mode: str = "solo",
    ) -> list[ArtifactRecord]:
        project = require_project_id(project_id)
        if not 40.0 <= bpm <= 240.0:
            raise ValueError("bpm must be between 40 and 240")
        instrumental_record, instrumental = self.store.read(instrumental_asset_id, project)
        vocal_record, vocal = self.store.read(vocal_asset_id, project)
        if backing_vocal_asset_id is not None:
            if backing_vocal_asset_id in {instrumental_asset_id, vocal_asset_id}:
                raise ValueError("backing_vocal_asset_id must refer to an independent artifact")
            if not -24.0 <= backing_gain_db <= 6.0:
                raise ValueError("backing_gain_db must be between -24 and 6")
            backing_record, backing = self.store.read(backing_vocal_asset_id, project)
            if backing_record.filename in {"vocal_wet_original.wav", "vocal_reverb_original.wav"}:
                raise ValueError("wet vocal and reverb residual are not independent backing vocals")
        part_ids = additional_vocal_asset_ids or []
        gains = additional_vocal_gains_db if additional_vocal_gains_db is not None else [-3.0] * len(part_ids)
        expected_counts = {"solo": 0, "duet": 1}
        if vocal_mode in expected_counts and len(part_ids) != expected_counts[vocal_mode]:
            raise ValueError("vocal_mode and additional vocal count do not match")
        if vocal_mode == "choir" and not 2 <= len(part_ids) <= 7 or vocal_mode not in {"solo", "duet", "choir"}:
            raise ValueError("vocal_mode and additional vocal count do not match")
        if len(gains) != len(part_ids) or any(not math.isfinite(gain) or not -24.0 <= gain <= 6.0 for gain in gains):
            raise ValueError("additional vocal gains must match tracks and be between -24 and 6 dB")
        all_ids = part_ids + [instrumental_asset_id, vocal_asset_id]
        if backing_vocal_asset_id is not None:
            all_ids.append(backing_vocal_asset_id)
        if len(set(all_ids)) != len(all_ids):
            raise ValueError("vocal parts must use distinct artifacts")
        parts = [self.store.read(part_id, project) for part_id in part_ids]
        if not lyrics_lrc.strip() or len(lyrics_lrc.encode("utf-8")) > 1024 * 1024:
            raise ValueError("lyrics_lrc must contain 1 byte to 1 MiB of UTF-8 text")
        files = {
            "instrumental": (instrumental_record.filename, instrumental, instrumental_record.media_type),
            "vocal": (vocal_record.filename, vocal, vocal_record.media_type),
            "lyrics_lrc": ("lyrics.lrc", lyrics_lrc.encode("utf-8"), "text/plain; charset=utf-8"),
        }
        data = {"bpm": str(bpm)}
        if backing_vocal_asset_id is not None:
            files["backing_vocal"] = (backing_record.filename, backing, backing_record.media_type)
            data["backing_gain_db"] = str(backing_gain_db)
        upload_files = list(files.items())
        if part_ids:
            data["vocal_mode"] = vocal_mode
            for (record, payload), gain in zip(parts, gains):
                upload_files.append(("additional_vocals", (record.filename, payload, record.media_type)))
            data["additional_vocal_gains_db"] = [str(gain) for gain in gains]
        archive = self._post(
            "Mixer",
            f"{self.service_urls.mixer}/v1/audio/masters",
            files=upload_files,
            data=data,
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
