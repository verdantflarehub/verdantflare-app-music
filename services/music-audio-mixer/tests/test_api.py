import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from verdantflare_mixer import api
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class MixerAPITest(unittest.TestCase):
    def test_choir_multipart_keeps_each_vocal_and_gain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.zip"
            result.write_bytes(b"archive")
            files = [
                ("instrumental", ("instrumental.wav", b"music", "audio/wav")),
                ("vocal", ("lead.wav", b"lead", "audio/wav")),
                ("lyrics_lrc", ("lyrics.lrc", b"[00:00.000]line\n", "text/plain")),
                ("additional_vocals", ("second.wav", b"second", "audio/wav")),
                ("additional_vocals", ("third.wav", b"third", "audio/wav")),
            ]
            with patch.object(api, "TEMP_ROOT", root), patch.object(api.service, "master", return_value=result) as master:
                response = TestClient(api.app).post(
                    "/v1/audio/masters",
                    data={"bpm": "120", "vocal_mode": "choir", "additional_vocal_gains_db": ["-3", "-6"]},
                    files=files,
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"archive")
            arguments = master.call_args.kwargs
            self.assertEqual(arguments["vocal_mode"], "choir")
            self.assertEqual(arguments["additional_vocal_gains_db"], [-3.0, -6.0])
            self.assertEqual(len(arguments["additional_vocals"]), 2)


if __name__ == "__main__":
    unittest.main()
