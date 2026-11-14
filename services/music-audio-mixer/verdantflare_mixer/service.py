from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path

from .graph import build_mix_graph, validate_arrangement


class MasteringFailed(RuntimeError):
    pass


class InvalidVocalArrangement(ValueError):
    pass


def _run(arguments: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
    )


def _loudness_measurement(path: Path) -> dict[str, str]:
    result = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-14:TP=-1:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture=True,
    )
    matches = re.findall(r"\{[^{}]+\}", result.stderr, re.DOTALL)
    if not matches:
        raise MasteringFailed("FFmpeg did not return loudness measurements")
    values = json.loads(matches[-1])
    required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not required.issubset(values):
        raise MasteringFailed("FFmpeg returned incomplete loudness measurements")
    return values


class MixerService:
    @staticmethod
    def _duration(path: Path) -> float:
        import soundfile as sf

        return sf.info(path).duration

    @staticmethod
    def _mix_filter(backing_gain_db: float | None, additional_gains_db: tuple[float, ...] = ()) -> str:
        return build_mix_graph(backing_gain_db, additional_gains_db)

    @staticmethod
    def _prepare_audio(source: Path, destination: Path) -> None:
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_f32le",
                str(destination),
            ]
        )

    @staticmethod
    def _process_vocal(source: Path, destination: Path, bpm: float) -> None:
        import soundfile as sf
        from pedalboard import Compressor, Delay, HighpassFilter, PeakFilter, Pedalboard, Reverb

        audio, sample_rate = sf.read(source, dtype="float32", always_2d=True)
        board = Pedalboard(
            [
                HighpassFilter(cutoff_frequency_hz=80.0),
                PeakFilter(cutoff_frequency_hz=3500.0, gain_db=2.0, q=0.8),
                Compressor(threshold_db=-18.0, ratio=3.0, attack_ms=10.0, release_ms=100.0),
                Delay(delay_seconds=60.0 / bpm / 4.0, feedback=0.12, mix=0.05),
                Reverb(room_size=0.18, damping=0.65, wet_level=0.08, dry_level=0.92),
            ]
        )
        processed = board(audio.T, sample_rate).T
        sf.write(destination, processed, sample_rate, subtype="FLOAT")

    def master(
        self,
        *,
        instrumental: Path,
        vocal: Path,
        backing_vocal: Path | None = None,
        backing_gain_db: float = -6.0,
        additional_vocals: list[Path] | None = None,
        additional_vocal_gains_db: list[float] | None = None,
        vocal_mode: str = "solo",
        lrc_text: str,
        bpm: float,
        work_directory: Path,
    ) -> Path:
        prepared_instrumental = work_directory / "instrumental-prepared.wav"
        prepared_vocal = work_directory / "vocal-prepared.wav"
        processed_vocal = work_directory / "vocal-processed.wav"
        prepared_backing = work_directory / "backing-prepared.wav"
        additional_vocals = additional_vocals or []
        additional_vocal_gains_db = additional_vocal_gains_db if additional_vocal_gains_db is not None else [-3.0] * len(additional_vocals)
        mixed = work_directory / "mixed.wav"
        master = work_directory / "Final_Song_Master.wav"
        mp3 = work_directory / "Final_Song.mp3"
        lrc = work_directory / "Final_Song.lrc"

        try:
            try:
                validate_arrangement(vocal_mode, len(additional_vocals), additional_vocal_gains_db)
            except ValueError as error:
                raise InvalidVocalArrangement(str(error)) from error
            if backing_vocal is not None and not -24.0 <= backing_gain_db <= 6.0:
                raise InvalidVocalArrangement("backing_gain_db must be between -24 and 6")
            self._prepare_audio(instrumental, prepared_instrumental)
            self._prepare_audio(vocal, prepared_vocal)
            self._process_vocal(prepared_vocal, processed_vocal, bpm)
            instrumental_duration = self._duration(prepared_instrumental)
            if abs(instrumental_duration - self._duration(prepared_vocal)) > 0.25:
                raise InvalidVocalArrangement("lead vocal duration differs from instrumental")
            if backing_vocal is not None:
                self._prepare_audio(backing_vocal, prepared_backing)
                backing_duration = self._duration(prepared_backing)
                if abs(instrumental_duration - backing_duration) > 0.25:
                    raise InvalidVocalArrangement("backing vocal duration differs from instrumental")
            mix_inputs = ["-i", str(prepared_instrumental), "-i", str(processed_vocal)]
            if backing_vocal is not None:
                mix_inputs.extend(["-i", str(prepared_backing)])
            for number, source in enumerate(additional_vocals, start=1):
                prepared = work_directory / f"vocal-part-{number}-prepared.wav"
                processed = work_directory / f"vocal-part-{number}-processed.wav"
                self._prepare_audio(source, prepared)
                if abs(instrumental_duration - self._duration(prepared)) > 0.25:
                    raise InvalidVocalArrangement(f"vocal part {number} duration differs from instrumental")
                self._process_vocal(prepared, processed, bpm)
                mix_inputs.extend(["-i", str(processed)])
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    *mix_inputs,
                    "-filter_complex",
                    self._mix_filter(backing_gain_db if backing_vocal is not None else None, tuple(additional_vocal_gains_db)),
                    "-map",
                    "[mix]",
                    "-c:a",
                    "pcm_f32le",
                    str(mixed),
                ]
            )
            measured = _loudness_measurement(mixed)
            loudnorm = (
                "loudnorm=I=-14:TP=-1:LRA=11:linear=true:"
                f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                f"measured_LRA={measured['input_lra']}:"
                f"measured_thresh={measured['input_thresh']}:"
                f"offset={measured['target_offset']}"
            )
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(mixed),
                    "-af",
                    loudnorm,
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s24le",
                    str(master),
                ]
            )
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(master),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "320k",
                    str(mp3),
                ]
            )
        except InvalidVocalArrangement:
            raise
        except (subprocess.CalledProcessError, OSError, ValueError) as error:
            raise MasteringFailed("audio mastering failed") from error

        lrc.write_text(lrc_text, encoding="utf-8")
        manifest = {
            "bpm": bpm,
            "backing_vocal": backing_vocal is not None,
            "backing_gain_db": backing_gain_db if backing_vocal is not None else None,
            "vocal_mode": vocal_mode,
            "additional_vocal_count": len(additional_vocals),
            "additional_vocal_gains_db": additional_vocal_gains_db,
            "target_lufs": -14,
            "true_peak_db": -1,
            "sample_rate": 48000,
            "master_bit_depth": 24,
            "mp3_bitrate_kbps": 320,
            "lrc_alignment": "supplied",
            "files": [master.name, mp3.name, lrc.name],
        }
        archive_path = work_directory / "final-song.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in (master, mp3, lrc):
                archive.write(path, path.name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        return archive_path
