import tempfile
import unittest
from pathlib import Path

from verdantflare_music_mcp.artifacts import ArtifactError, ArtifactNotFound, ArtifactStore


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

    def test_streamed_create_is_atomic_and_verifies_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            record = store.create_from_chunks(
                project_id="project",
                operation="asset.import",
                filename="source.mp3",
                media_type="audio/mpeg",
                chunks=(b"source-", b"audio"),
                expected_sha256="2578ea4ee8aa86428a0bb186f0a10b576a608fe22921b8d903f684443b7fe170",
            )
            self.assertEqual(store.read(record.artifact_id, "project")[1], b"source-audio")

            with self.assertRaisesRegex(ArtifactError, "SHA-256"):
                store.create_from_chunks(
                    project_id="project",
                    operation="asset.import",
                    filename="source.mp3",
                    media_type="audio/mpeg",
                    chunks=(b"different",),
                    expected_sha256="0" * 64,
                )
            entries = [path for path in (Path(directory) / "artifacts").iterdir() if path.is_dir()]
            self.assertEqual(entries, [Path(directory) / "artifacts" / record.artifact_id])


if __name__ == "__main__":
    unittest.main()
