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
            self.assertEqual(UVR5Service._find_output([str(output)], "vocal_dry"), output)

    def test_rejects_missing_named_output(self) -> None:
        with self.assertRaises(SeparationFailed):
            UVR5Service._find_output([], "vocal_dry")

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


if __name__ == "__main__":
    unittest.main()
