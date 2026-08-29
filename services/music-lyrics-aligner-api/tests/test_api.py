from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from verdantflare_lyrics_aligner import api


class FakeService:
    model_name = "small"
    language = "zh"
    ready = True

    def load(self) -> None:
        pass

    def align(self, source: Path, lyrics: bytes, work_directory: Path) -> str:
        self.audio = source.read_bytes()
        self.lyrics = lyrics
        return "[00:01.000]第一句\n"


class APITest(unittest.TestCase):
    def test_health_and_alignment_contract(self) -> None:
        fake = FakeService()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(api, "service", fake),
            mock.patch.object(api, "TEMP_ROOT", Path(directory)),
            TestClient(api.app) as client,
        ):
            self.assertEqual(client.get("/health").json()["backend"], "stable-ts-2.19.1")
            response = client.post(
                "/v1/lyrics/alignments",
                files={"audio": ("vocal.wav", b"audio", "audio/wav"), "lyrics": ("lyrics.txt", "第一句".encode())},
                data={"language": "zh"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), "[00:01.000]第一句\n")
        self.assertEqual(fake.audio, b"audio")
        self.assertEqual(fake.lyrics, "第一句".encode())

    def test_rejects_unsupported_language_and_empty_upload(self) -> None:
        fake = FakeService()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(api, "service", fake),
            mock.patch.object(api, "TEMP_ROOT", Path(directory)),
            TestClient(api.app) as client,
        ):
            unsupported = client.post(
                "/v1/lyrics/alignments",
                files={"audio": ("vocal.wav", b"audio"), "lyrics": ("lyrics.txt", b"line")},
                data={"language": "en"},
            )
            empty = client.post(
                "/v1/lyrics/alignments",
                files={"audio": ("vocal.wav", b""), "lyrics": ("lyrics.txt", b"line")},
                data={"language": "zh"},
            )
        self.assertEqual(unsupported.status_code, 422)
        self.assertEqual(empty.status_code, 422)


if __name__ == "__main__":
    unittest.main()
