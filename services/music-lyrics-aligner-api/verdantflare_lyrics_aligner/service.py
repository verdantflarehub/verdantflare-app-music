from __future__ import annotations

import math
import re
import subprocess
import threading
import wave
from pathlib import Path
from typing import Callable


LRC_TIMESTAMP = re.compile(r"^\s*\[\d{1,3}:\d{2}(?:[.:]\d{1,3})\]")
SECTION_LABEL = re.compile(r"^\s*\[[^\]]+\]\s*$")


class InvalidAlignmentInput(ValueError):
    pass


class AlignmentFailed(RuntimeError):
    pass


class ModelUnavailable(RuntimeError):
    pass


def parse_lyrics(payload: bytes) -> list[str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidAlignmentInput("lyrics must be UTF-8") from error

    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        raise InvalidAlignmentInput("lyrics must contain at least one non-empty line")
    if any(LRC_TIMESTAMP.match(line) for line in lines):
        raise InvalidAlignmentInput("lyrics must not contain LRC timestamps")
    if any(SECTION_LABEL.match(line) for line in lines):
        raise InvalidAlignmentInput("lyrics must not contain section labels")
    return lines


def format_lrc(lines: list[str], segments: list[object], duration_seconds: float) -> str:
    if len(segments) != len(lines):
        raise AlignmentFailed("aligner did not preserve the lyric line count")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise AlignmentFailed("decoded audio duration is invalid")

    timestamps: list[int] = []
    for segment in segments:
        words = getattr(segment, "words", None)
        if not words:
            raise AlignmentFailed("a lyric line has no aligned words")
        start = float(getattr(words[0], "start", math.nan))
        end = float(getattr(words[-1], "end", math.nan))
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise AlignmentFailed("a lyric line has an invalid aligned duration")
        timestamp = round(start * 1000)
        if timestamp > round(duration_seconds * 1000):
            raise AlignmentFailed("a lyric timestamp exceeds the audio duration")
        timestamps.append(timestamp)

    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise AlignmentFailed("lyric timestamps must be strictly increasing")

    output = []
    for timestamp, line in zip(timestamps, lines):
        minutes, remainder = divmod(timestamp, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        output.append(f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]{line}")
    return "\n".join(output) + "\n"


class AlignmentService:
    def __init__(
        self,
        model_root: Path,
        model_name: str = "small",
        language: str = "zh",
        model_loader: Callable[..., object] | None = None,
        cuda_available: Callable[[], bool] | None = None,
    ) -> None:
        self.model_root = model_root
        self.model_name = model_name
        self.language = language
        self._model_loader = model_loader
        self._cuda_available = cuda_available
        self._model: object | None = None
        self._load_error: str | None = None
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def load(self) -> None:
        try:
            if self._cuda_available is None:
                import torch

                cuda_available = torch.cuda.is_available
            else:
                cuda_available = self._cuda_available
            if not cuda_available():
                raise ModelUnavailable("CUDA is unavailable")

            if self._model_loader is None:
                import stable_whisper

                model_loader = stable_whisper.load_model
            else:
                model_loader = self._model_loader
            self.model_root.mkdir(parents=True, exist_ok=True)
            self._model = model_loader(self.model_name, device="cuda", download_root=str(self.model_root))
            self._load_error = None
        except Exception as error:
            self._model = None
            self._load_error = str(error)
            if isinstance(error, ModelUnavailable):
                raise
            raise ModelUnavailable("failed to load the alignment model") from error

    @staticmethod
    def _decode_audio(source: Path, destination: Path) -> float:
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-c:a",
                    "pcm_s16le",
                    str(destination),
                ],
                check=True,
            )
            with wave.open(str(destination), "rb") as audio:
                return audio.getnframes() / audio.getframerate()
        except (OSError, subprocess.CalledProcessError, wave.Error, ZeroDivisionError) as error:
            raise InvalidAlignmentInput("audio could not be decoded") from error

    def align(self, source: Path, lyrics_payload: bytes, work_directory: Path) -> str:
        if self._model is None:
            raise ModelUnavailable(self._load_error or "alignment model is unavailable")
        lines = parse_lyrics(lyrics_payload)
        decoded = work_directory / "vocal-16k.wav"
        duration = self._decode_audio(source, decoded)
        try:
            with self._lock:
                result = self._model.align(
                    str(decoded),
                    "\n".join(lines),
                    language=self.language,
                    original_split=True,
                    failure_threshold=0.0,
                    verbose=None,
                )
        except Exception as error:
            raise AlignmentFailed("known-lyrics alignment failed") from error
        return format_lrc(lines, list(getattr(result, "segments", [])), duration)
