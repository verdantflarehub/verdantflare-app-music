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
    def __init__(
        self,
        model_root: Path,
        backing_model: str | None = None,
        backing_lead_stem: str | None = None,
        backing_vocal_stem: str | None = None,
    ) -> None:
        self.model_root = model_root
        if any((backing_model, backing_lead_stem, backing_vocal_stem)) and not all((backing_model, backing_lead_stem, backing_vocal_stem)):
            raise ValueError("backing model filename and both source stem names must be configured together")
        if backing_model is not None and (Path(backing_model).name != backing_model or backing_lead_stem == backing_vocal_stem):
            raise ValueError("backing model filename must be safe and source stems must be distinct")
        self.backing_model = backing_model
        self.backing_lead_stem = backing_lead_stem
        self.backing_vocal_stem = backing_vocal_stem

    def _separator(self, output_directory: Path):
        from audio_separator.separator import Separator

        return Separator(
            model_file_dir=str(self.model_root),
            output_dir=str(output_directory),
            output_format="WAV",
            sample_rate=48000,
            use_soundfile=False,
            use_autocast=True,
        )

    @staticmethod
    def _find_output(paths: list[str], expected_stem: str, output_directory: Path) -> Path:
        expected = expected_stem.casefold()
        candidates = [
            path if path.is_absolute() else output_directory / path
            for value in paths
            if expected in (path := Path(value)).stem.casefold()
        ]
        matches = [path for path in candidates if path.is_file()]
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
                    "Other": "instrumental_raw",
                },
            )
            vocal_wet = self._find_output(separated, "vocal_wet", work_directory)
            instrumental_raw = self._find_output(separated, "instrumental_raw", work_directory)

            separator.load_model(model_filename=DEREVERB_MODEL)
            dereverbed = separator.separate(
                str(vocal_wet),
                custom_output_names={
                    "Noreverb": "vocal_dry",
                    "Reverb": "discarded_reverb",
                },
            )
            vocal_dry = self._find_output(dereverbed, "vocal_dry", work_directory)
            vocal_reverb = self._find_output(dereverbed, "discarded_reverb", work_directory)
            if self.backing_model is not None:
                if not (self.model_root / self.backing_model).is_file():
                    raise SeparationFailed("configured backing model is not installed")
                separator.load_model(model_filename=self.backing_model)
                backing_outputs = separator.separate(
                    str(vocal_wet),
                    custom_output_names={
                        self.backing_lead_stem: "vocal_lead_reference",
                        self.backing_vocal_stem: "backing_vocals_unreviewed",
                    },
                )
                lead_reference_raw = self._find_output(backing_outputs, "vocal_lead_reference", work_directory)
                backing_raw = self._find_output(backing_outputs, "backing_vocals_unreviewed", work_directory)

        instrumental = work_directory / "instrumental.wav"
        vocal = work_directory / "vocal_dry_original.wav"
        wet = work_directory / "vocal_wet_original.wav"
        reverb = work_directory / "vocal_reverb_original.wav"
        extra_files: list[Path] = []
        try:
            self._normalize(instrumental_raw, instrumental)
            self._normalize(vocal_dry, vocal)
            self._normalize(vocal_wet, wet)
            self._normalize(vocal_reverb, reverb)
            if self.backing_model is not None:
                lead_reference = work_directory / "vocal_lead_reference.wav"
                backing = work_directory / "backing_vocals_unreviewed.wav"
                self._normalize(lead_reference_raw, lead_reference)
                self._normalize(backing_raw, backing)
                extra_files = [lead_reference, backing]
        except subprocess.CalledProcessError as error:
            raise SeparationFailed("failed to encode separated stems") from error

        manifest = {
            "sample_rate": 48000,
            "bit_depth": 24,
            "separation_model": SEPARATION_MODEL,
            "dereverb_model": DEREVERB_MODEL,
            "backing_model": self.backing_model,
            "backing_requires_review": self.backing_model is not None,
            "files": [path.name for path in (instrumental, vocal, wet, reverb, *extra_files)],
        }
        archive_path = work_directory / "stems.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(instrumental, instrumental.name)
            archive.write(vocal, vocal.name)
            archive.write(wet, wet.name)
            archive.write(reverb, reverb.name)
            for path in extra_files:
                archive.write(path, path.name)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        return archive_path
