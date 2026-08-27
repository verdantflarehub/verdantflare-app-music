from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from verdantflare_uvr5.service import SEPARATION_MODEL, SeparationFailed, UVR5Service


class UVR5ServiceTest(unittest.TestCase):
    def test_uses_upstream_big_beta_4_filename(self):
        self.assertEqual(SEPARATION_MODEL, "melband_roformer_big_beta4.ckpt")

    def test_finds_named_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "song_(vocal_dry).wav"
            output.touch()
            self.assertEqual(
                UVR5Service._find_output([output.name], "vocal_dry", output.parent),
                output,
            )

    def test_rejects_missing_named_output(self) -> None:
        with self.assertRaises(SeparationFailed):
            UVR5Service._find_output([], "vocal_dry", Path("/output"))

    def test_uses_ffmpeg_writer_for_compressed_inputs(self) -> None:
        separator_module = types.ModuleType("audio_separator.separator")

        class FakeSeparator:
            def __init__(self, **kwargs):
                self.options = kwargs

        separator_module.Separator = FakeSeparator
        package = types.ModuleType("audio_separator")
        package.separator = separator_module

        with patch.dict(
            "sys.modules",
            {
                "audio_separator": package,
                "audio_separator.separator": separator_module,
            },
        ):
            separator = UVR5Service(Path("/models"))._separator(Path("/output"))

        self.assertFalse(separator.options["use_soundfile"])

    def test_uses_model_stem_names_for_both_stages(self) -> None:
        calls = []

        class FakeSeparator:
            def load_model(self, model_filename):
                pass

            def separate(self, source, custom_output_names):
                calls.append(custom_output_names)
                outputs = []
                for output_name in custom_output_names.values():
                    output = Path(source).parent / f"{output_name}.wav"
                    output.touch()
                    outputs.append(output.name)
                return outputs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.mp3"
            source.touch()
            service = UVR5Service(Path("/models"))
            with (
                patch.object(service, "_separator", return_value=FakeSeparator()),
                patch.object(service, "_normalize", side_effect=lambda _, output: output.touch()),
            ):
                archive = service.separate(source, root)

            self.assertTrue(archive.is_file())

        self.assertEqual(
            calls,
            [
                {"Vocals": "vocal_wet", "Other": "instrumental_raw"},
                {"Noreverb": "vocal_dry", "Reverb": "discarded_reverb"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
