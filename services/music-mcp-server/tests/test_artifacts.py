import tempfile
import unittest
from pathlib import Path

from verdantflare_music_mcp.artifacts import ArtifactNotFound, ArtifactStore


class ArtifactStoreTest(unittest.TestCase):
    def test_create_read_and_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            record = store.create(
                project_id="mengsk-error",
                operation="music.generate",
                filename="candidate.wav",
                media_type="audio/wav",
                payload=b"audio",
            )
            loaded, payload = store.read(record.artifact_id, "mengsk-error")
            self.assertEqual(payload, b"audio")
            self.assertEqual(loaded.sha256, record.sha256)
            self.assertEqual(
                store.download_url(record.artifact_id),
                f"https://music.example/artifacts/{record.artifact_id}/content",
            )
            with self.assertRaises(ArtifactNotFound):
                store.read(record.artifact_id, "another-project")

    def test_rejects_unsafe_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            with self.assertRaises(ValueError):
                store.create(
                    project_id="../outside",
                    operation="test",
                    filename="audio.wav",
                    media_type="audio/wav",
                    payload=b"audio",
                )
            with self.assertRaises(ValueError):
                store.create(
                    project_id="project",
                    operation="test",
                    filename="../audio.wav",
                    media_type="audio/wav",
                    payload=b"audio",
                )


if __name__ == "__main__":
    unittest.main()
