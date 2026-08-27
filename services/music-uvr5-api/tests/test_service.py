from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
