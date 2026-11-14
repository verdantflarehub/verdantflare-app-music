from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
import zipfile
from array import array
from dataclasses import dataclass
from pathlib import Path


SAMPLE_RATE = 40000
SAMPLE_WIDTH = 2
CHANNELS = 1
SILENCE_THRESHOLD_DBFS = -50.0
MINIMUM_SILENCE_SECONDS = 1.0
RETAINED_SILENCE_SECONDS = 0.8
BRIDGED_ACTIVITY_SECONDS = 0.2
ANALYSIS_WINDOW_SECONDS = 0.02
TARGET_SAMPLE_PEAK_DBFS = -2.0


class PreparationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparationSource:
    filename: str
    path: Path
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def _decode(source_path: Path, output_path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        check=False,
    )
    if result.returncode:
        raise PreparationFailed("recording could not be decoded")


def _verify_pcm(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getframerate() != SAMPLE_RATE
                or source.getsampwidth() != SAMPLE_WIDTH
                or source.getnchannels() != CHANNELS
                or source.getcomptype() != "NONE"
                or source.getnframes() == 0
            ):
                raise PreparationFailed("prepared audio is not non-empty 40 kHz mono PCM16 WAV")
            return source.getnframes(), source.getframerate()
    except (EOFError, wave.Error) as error:
        raise PreparationFailed("prepared audio is not a valid WAV") from error


def _pcm16_samples(payload: bytes) -> array[int]:
    samples = array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _pcm16_bytes(samples: array[int]) -> bytes:
    if sys.byteorder == "little":
        return samples.tobytes()
    little_endian = array("h", samples)
    little_endian.byteswap()
    return little_endian.tobytes()


def _pcm_peak(path: Path) -> int:
    peak = 0
    with wave.open(str(path), "rb") as source:
        while chunk := source.readframes(SAMPLE_RATE * 10):
            peak = max(peak, max((abs(sample) for sample in _pcm16_samples(chunk)), default=0))
    return peak


def _scale_pcm(source_path: Path, output_path: Path, gain: float) -> None:
    with wave.open(str(source_path), "rb") as source, wave.open(str(output_path), "wb") as output:
        output.setparams(source.getparams())
        while chunk := source.readframes(SAMPLE_RATE * 10):
            scaled = array(
                "h",
                (
                    max(-32768, min(32767, round(sample * gain)))
                    for sample in _pcm16_samples(chunk)
                ),
            )
            output.writeframes(_pcm16_bytes(scaled))


def _quiet_windows(path: Path) -> tuple[list[bool], int, int]:
    window_frames = round(SAMPLE_RATE * ANALYSIS_WINDOW_SECONDS)
    threshold = ((1 << 15) - 1) * 10 ** (SILENCE_THRESHOLD_DBFS / 20)
    quiet: list[bool] = []
    with wave.open(str(path), "rb") as source:
        total_frames = source.getnframes()
        while chunk := source.readframes(window_frames):
            samples = _pcm16_samples(chunk)
            rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            quiet.append(rms < threshold)
    return quiet, window_frames, total_frames


def _long_silence_runs(quiet: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_quiet in enumerate(quiet + [False]):
        if is_quiet and start is None:
            start = index
        elif not is_quiet and start is not None:
            runs.append((start, index))
            start = None

    bridge_windows = round(BRIDGED_ACTIVITY_SECONDS / ANALYSIS_WINDOW_SECONDS)
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] <= bridge_windows:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    minimum_windows = math.ceil(MINIMUM_SILENCE_SECONDS / ANALYSIS_WINDOW_SECONDS)
    return [(start, end) for start, end in merged if end - start >= minimum_windows]


def _kept_ranges(path: Path) -> tuple[list[tuple[int, int]], list[tuple[float, float]]]:
    quiet, window_frames, total_frames = _quiet_windows(path)
    if all(quiet):
        raise PreparationFailed("recording contains no audio above the silence threshold")
    runs = _long_silence_runs(quiet)
    keep_side_frames = round(RETAINED_SILENCE_SECONDS * SAMPLE_RATE / 2)
    current_start = 0
    final_end = total_frames
    ranges: list[tuple[int, int]] = []
    removed: list[tuple[float, float]] = []

    for start_window, end_window in runs:
        silence_start = min(total_frames, start_window * window_frames)
        silence_end = min(total_frames, end_window * window_frames)
        if silence_start == 0:
            current_start = max(current_start, silence_end - keep_side_frames)
            removed.append((0.0, current_start / SAMPLE_RATE))
            continue
        if silence_end >= total_frames:
            final_end = min(final_end, silence_start + keep_side_frames)
            removed.append((final_end / SAMPLE_RATE, total_frames / SAMPLE_RATE))
            continue

        range_end = min(total_frames, silence_start + keep_side_frames)
        next_start = max(0, silence_end - keep_side_frames)
        if range_end > current_start:
            ranges.append((current_start, range_end))
        removed.append((range_end / SAMPLE_RATE, next_start / SAMPLE_RATE))
        current_start = next_start

    if final_end > current_start:
        ranges.append((current_start, final_end))
    ranges = [(start, end) for start, end in ranges if end > start]
    if not ranges:
        raise PreparationFailed("silence removal produced no usable audio")
    return ranges, removed


def _write_range(source_path: Path, output_path: Path, start: int, end: int) -> None:
    with wave.open(str(source_path), "rb") as source, wave.open(str(output_path), "wb") as output:
        output.setparams(source.getparams())
        source.setpos(start)
        remaining = end - start
        while remaining:
            count = min(remaining, SAMPLE_RATE * 10)
            chunk = source.readframes(count)
            if not chunk:
                raise PreparationFailed("prepared audio ended before a segment boundary")
            frames = len(chunk) // SAMPLE_WIDTH
            output.writeframes(chunk)
            remaining -= frames


def _concatenate(paths: list[Path], output_path: Path) -> None:
    if not paths:
        raise PreparationFailed("no audio segments were produced")
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(SAMPLE_RATE)
        for path in paths:
            _verify_pcm(path)
            with wave.open(str(path), "rb") as source:
                while chunk := source.readframes(SAMPLE_RATE * 10):
                    output.writeframes(chunk)


def _maximum_silence_seconds(path: Path) -> float:
    quiet, _, _ = _quiet_windows(path)
    longest = current = 0
    for is_quiet in quiet:
        current = current + 1 if is_quiet else 0
        longest = max(longest, current)
    return longest * ANALYSIS_WINDOW_SECONDS


def _probe(path: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate,format_name:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise PreparationFailed("ffprobe could not inspect an input recording")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PreparationFailed("ffprobe returned invalid metadata") from error


def _loudness(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23:TP=-2:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise PreparationFailed("ffmpeg could not measure prepared audio")
    start = result.stderr.rfind("{")
    try:
        measured = json.loads(result.stderr[start:])
        return {
            "integrated_lufs": float(measured["input_i"]),
            "true_peak_dbtp": float(measured["input_tp"]),
            "loudness_range_lu": float(measured["input_lra"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PreparationFailed("ffmpeg returned invalid loudness metrics") from error


def _pitch(path: Path) -> dict[str, object]:
    import librosa
    import numpy as np

    audio, _ = librosa.load(path, sr=16000, mono=True)
    f0, voiced, probability = librosa.pyin(
        audio,
        fmin=librosa.note_to_hz("C2"),
        fmax=librosa.note_to_hz("C6"),
        sr=16000,
        frame_length=2048,
        hop_length=320,
        fill_na=np.nan,
    )
    rms = librosa.feature.rms(y=audio, frame_length=2048, hop_length=320)[0]
    count = min(len(f0), len(rms))
    f0, voiced, probability, rms = (
        f0[:count],
        voiced[:count],
        probability[:count],
        rms[:count],
    )
    db = librosa.amplitude_to_db(np.maximum(rms, 1e-10), ref=1.0)
    reliable = np.isfinite(f0) & voiced & (probability >= 0.8) & (db >= -45.0)
    values = f0[reliable]
    if not len(values):
        return {"reliable_voiced_seconds": 0.0, "f0_percentiles_hz": {}, "high_pitch_seconds": {}}
    percentiles = np.percentile(values, [5, 25, 50, 75, 95, 99])
    high_pitch = {}
    for note, frequency in {
        "C4": 261.626,
        "E4": 329.628,
        "G4": 391.995,
        "A4": 440.0,
        "B4": 493.883,
        "C5": 523.251,
    }.items():
        high_pitch[note] = round(float(np.sum(values >= frequency) * 0.02), 2)
    return {
        "reliable_voiced_seconds": round(float(np.sum(reliable) * 0.02), 2),
        "f0_percentiles_hz": {
            label: round(float(value), 1)
            for label, value in zip(("p5", "p25", "p50", "p75", "p95", "p99"), percentiles)
        },
        "high_pitch_seconds": high_pitch,
    }


class VoiceDatasetPreparer:
    def prepare(self, sources: list[PreparationSource], work_root: Path, archive_path: Path) -> None:
        if not 1 <= len(sources) <= 20:
            raise PreparationFailed("voice preparation requires 1-20 recordings")
        try:
            work_root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise PreparationFailed("voice preparation work directory already exists") from error
        prepared_root = work_root / "prepared"
        segments_root = work_root / "segments"
        prepared_root.mkdir()
        segments_root.mkdir()
        prepared_paths: list[Path] = []
        source_reports: list[dict[str, object]] = []
        segment_manifest: list[dict[str, object]] = []

        for source_number, source in enumerate(sources, start=1):
            decoded = work_root / f"decoded-{source_number:02d}.wav"
            try:
                _decode(source.path, decoded)
            except PreparationFailed as error:
                raise PreparationFailed(f"recording {source_number} could not be decoded") from error
            _verify_pcm(decoded)
            peak = _pcm_peak(decoded)
            target_peak = ((1 << 15) - 1) * 10 ** (TARGET_SAMPLE_PEAK_DBFS / 20)
            gain = min(1.0, target_peak / peak) if peak else 1.0
            normalized = work_root / f"normalized-{source_number:02d}.wav"
            if gain < 1.0:
                _scale_pcm(decoded, normalized, gain)
            else:
                shutil.copyfile(decoded, normalized)

            ranges, removed = _kept_ranges(normalized)
            segment_paths: list[Path] = []
            segment_reports: list[dict[str, object]] = []
            for segment_number, (start, end) in enumerate(ranges, start=1):
                filename = f"{source_number:02d}-{segment_number:03d}.wav"
                segment_path = segments_root / filename
                _write_range(normalized, segment_path, start, end)
                segment_paths.append(segment_path)
                record = {
                    "filename": filename,
                    "source_start_seconds": round(start / SAMPLE_RATE, 4),
                    "source_end_seconds": round(end / SAMPLE_RATE, 4),
                    "duration_seconds": round((end - start) / SAMPLE_RATE, 4),
                    "sha256": _sha256(segment_path),
                }
                segment_reports.append(record)
                segment_manifest.append({"source_number": source_number, **record})

            prepared_filename = f"prepared-{source_number:02d}.wav"
            prepared_path = prepared_root / prepared_filename
            _concatenate(segment_paths, prepared_path)
            if _maximum_silence_seconds(prepared_path) > MINIMUM_SILENCE_SECONDS:
                raise PreparationFailed("prepared audio still contains silence longer than one second")
            prepared_paths.append(prepared_path)
            sample_peak = _pcm_peak(prepared_path)
            source_reports.append(
                {
                    "source_number": source_number,
                    "source_filename": source.filename,
                    "source_sha256": source.sha256,
                    "source_probe": _probe(source.path),
                    "prepared_filename": prepared_filename,
                    "prepared_sha256": _sha256(prepared_path),
                    "prepared_duration_seconds": round(_duration(prepared_path), 4),
                    "sample_peak_dbfs": round(20 * math.log10(max(sample_peak, 1) / 32767), 2),
                    "maximum_silence_seconds": round(_maximum_silence_seconds(prepared_path), 2),
                    "removed_ranges_seconds": [
                        {"start": round(start, 4), "end": round(end, 4)} for start, end in removed
                    ],
                    "segments": segment_reports,
                    "loudness": _loudness(prepared_path),
                    "pitch": _pitch(prepared_path),
                }
            )

        training_path = work_root / "voice-training.wav"
        _concatenate(prepared_paths, training_path)
        maximum_silence = _maximum_silence_seconds(training_path)
        if maximum_silence > MINIMUM_SILENCE_SECONDS:
            raise PreparationFailed("merged training audio contains silence longer than one second")

        report = {
            "schema_version": 1,
            "automatic_status": "passed",
            "review_required": True,
            "settings": {
                "sample_rate": SAMPLE_RATE,
                "sample_width_bits": SAMPLE_WIDTH * 8,
                "channels": CHANNELS,
                "silence_threshold_dbfs": SILENCE_THRESHOLD_DBFS,
                "minimum_silence_seconds": MINIMUM_SILENCE_SECONDS,
                "retained_silence_seconds": RETAINED_SILENCE_SECONDS,
                "bridged_activity_seconds": BRIDGED_ACTIVITY_SECONDS,
                "target_sample_peak_dbfs": TARGET_SAMPLE_PEAK_DBFS,
            },
            "sources": source_reports,
            "training": {
                "filename": training_path.name,
                "duration_seconds": round(_duration(training_path), 4),
                "sha256": _sha256(training_path),
                "maximum_silence_seconds": round(maximum_silence, 2),
                "loudness": _loudness(training_path),
                "pitch": _pitch(training_path),
            },
        }
        report_path = work_root / "voice-preparation-report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        segments_archive = work_root / "voice-segments.zip"
        with zipfile.ZipFile(segments_archive, "w", compression=zipfile.ZIP_STORED) as archive:
            for segment_path in sorted(segments_root.glob("*.wav")):
                archive.write(segment_path, segment_path.name)
            archive.writestr(
                "manifest.json",
                json.dumps({"schema_version": 1, "segments": segment_manifest}, indent=2) + "\n",
            )

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for prepared_path in prepared_paths:
                archive.write(prepared_path, prepared_path.name)
            archive.write(training_path, training_path.name)
            archive.write(report_path, report_path.name)
            archive.write(segments_archive, segments_archive.name)
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_count": len(sources),
                        "automatic_status": "passed",
                        "review_required": True,
                    },
                    indent=2,
                )
                + "\n",
            )


def prepare_voice_dataset_archive(
    sources: list[PreparationSource],
    work_root: Path,
    archive_path: Path,
) -> None:
    VoiceDatasetPreparer().prepare(sources, work_root, archive_path)
