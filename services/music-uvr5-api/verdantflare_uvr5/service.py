from __future__ import annotations

import json
import subprocess
import threading
import zipfile
from pathlib import Path


SEPARATION_MODEL = "melband_roformer_big_beta4.ckpt"
DEREVERB_MODEL = "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt"
GPU_LOCK = threading.Lock()


class SeparationFailed(RuntimeError):
    pass


class UVR5Service:
    def __init__(self, model_root: Path) -> None:
        self.model_root = model_root

    def _separator(self, output_directory: Path):
        from audio_separator.separator import Separator

        return Separator(
            model_file_dir=str(self.model_root),
            output_dir=str(output_directory),
            output_format="WAV",
            sample_rate=48000,
            use_soundfile=True,
            use_autocast=True,
        )

    @staticmethod
    def _find_output(paths: list[str], expected_stem: str) -> Path:
        expected = expected_stem.casefold()
        matches = [Path(path) for path in paths if expected in Path(path).stem.casefold()]
        if len(matches) != 1 or not matches[0].is_file():
            raise SeparationFailed(f"separator did not produce the {expected_stem} stem")
        return matches[0]

    @staticmethod
    def _normalize(source: Path, destination: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-ar",
                "48000",
                "-c:a",
                "pcm_s24le",
                str(destination),
            ],
            check=True,
        )

    def separate(self, source: Path, work_directory: Path) -> Path:
        with GPU_LOCK:
            separator = self._separator(work_directory)
            separator.load_model(model_filename=SEPARATION_MODEL)
            separated = separator.separate(
                str(source),
                custom_output_names={
                    "Vocals": "vocal_wet",
                    "Instrumental": "instrumental_raw",
                },
            )
            vocal_wet = self._find_output(separated, "vocal_wet")
            instrumental_raw = self._find_output(separated, "instrumental_raw")

            separator.load_model(model_filename=DEREVERB_MODEL)
            dereverbed = separator.separate(
                str(vocal_wet),
                custom_output_names={
                    "No Reverb": "vocal_dry",
                    "Reverb": "discarded_reverb",
                },
            )
            vocal_dry = self._find_output(dereverbed, "vocal_dry")

        instrumental = work_directory / "instrumental.wav"
        vocal = work_directory / "vocal_dry_original.wav"
        try:
            self._normalize(instrumental_raw, instrumental)
            self._normalize(vocal_dry, vocal)
        except subprocess.CalledProcessError as error:
            raise SeparationFailed("failed to encode separated stems") from error

        manifest = {
            "sample_rate": 48000,
            "bit_depth": 24,
            "separation_model": SEPARATION_MODEL,
            "dereverb_model": DEREVERB_MODEL,
            "files": [instrumental.name, vocal.name],
        }
        archive_path = work_directory / "stems.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(instrumental, instrumental.name)
            archive.write(vocal, vocal.name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        return archive_path
