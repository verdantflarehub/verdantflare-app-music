from __future__ import annotations

import json
import re
import subprocess
import zipfile
from pathlib import Path

import soundfile as sf
from pedalboard import Compressor, Delay, HighpassFilter, PeakFilter, Pedalboard, Reverb


class MasteringFailed(RuntimeError):
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
        lrc_text: str,
        bpm: float,
        work_directory: Path,
    ) -> Path:
        prepared_instrumental = work_directory / "instrumental-prepared.wav"
        prepared_vocal = work_directory / "vocal-prepared.wav"
        processed_vocal = work_directory / "vocal-processed.wav"
        mixed = work_directory / "mixed.wav"
        master = work_directory / "Final_Song_Master.wav"
        mp3 = work_directory / "Final_Song.mp3"
        lrc = work_directory / "Final_Song.lrc"

        try:
            self._prepare_audio(instrumental, prepared_instrumental)
            self._prepare_audio(vocal, prepared_vocal)
            self._process_vocal(prepared_vocal, processed_vocal, bpm)
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(prepared_instrumental),
                    "-i",
                    str(processed_vocal),
                    "-filter_complex",
                    "[0:a][1:a]sidechaincompress=threshold=0.04:ratio=3:attack=20:release=250[ducked];"
                    "[ducked][1:a]amix=inputs=2:duration=longest:normalize=0[mix]",
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
        except (subprocess.CalledProcessError, OSError, ValueError) as error:
            raise MasteringFailed("audio mastering failed") from error

        lrc.write_text(lrc_text, encoding="utf-8")
        manifest = {
            "bpm": bpm,
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
