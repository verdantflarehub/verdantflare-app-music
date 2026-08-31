from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from verdantflare_video_mcp.artifacts import ArtifactStore
from verdantflare_video_mcp.executor import VideoExecutor
from verdantflare_video_mcp.tasks import TaskConflict, TaskStore


class VideoMCPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.artifacts = ArtifactStore(root)
        self.tasks = TaskStore(root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _artifact(self, filename: str, media_type: str, payload: bytes = b"media"):
        return self.artifacts.create_from_chunks(project_id="promo-test", operation="test",
                                                 filename=filename, media_type=media_type, chunks=(payload,))

    def test_artifacts_are_project_scoped(self) -> None:
        artifact = self._artifact("identity.png", "image/png")
        self.assertEqual(self.artifacts.get(artifact.artifact_id, "promo-test").sha256, artifact.sha256)
        with self.assertRaises(Exception):
            self.artifacts.get(artifact.artifact_id, "another-project")

    def test_generate_is_idempotent_and_hides_runtime_task(self) -> None:
        image = self._artifact("identity.png", "image/png")

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/videos")
            body = json.loads(request.content)
            self.assertEqual(body["conditions"][0]["type"], "image")
            self.assertIn("<Picture 1>", body["prompt"])
            return httpx.Response(200, json={"id": "runtime-secret-id"})

        executor = VideoExecutor(self.artifacts, self.tasks, httpx.Client(transport=httpx.MockTransport(handler)))
        inputs = dict(project_id="promo-test", idempotency_key="PGU001_v1/attempt_01",
                      model="minimax-h3-ref2va", prompt="A valid single shot", duration_seconds=6,
                      aspect_ratio="9:16", references={"images": [{"artifact_id": image.artifact_id,
                                                                     "purpose": "identity"}],
                                                       "videos": [], "audios": []})
        first = executor.generate(**inputs)
        second = executor.generate(**inputs)
        self.assertEqual(first.video_task_id, second.video_task_id)
        self.assertEqual(first.runtime_task_id, "runtime-secret-id")
        self.assertNotIn("runtime-secret-id", first.video_task_id)

    def test_idempotency_key_rejects_changed_input(self) -> None:
        image = self._artifact("identity.png", "image/png")
        executor = VideoExecutor(self.artifacts, self.tasks,
                                 httpx.Client(transport=httpx.MockTransport(
                                     lambda request: httpx.Response(200, json={"id": "runtime-id"}))))
        common = dict(project_id="promo-test", idempotency_key="PGU001_v1/attempt_01",
                      model="minimax-h3-ref2va", duration_seconds=6, aspect_ratio="9:16",
                      references={"images": [{"artifact_id": image.artifact_id, "purpose": "identity"}],
                                  "videos": [], "audios": []})
        executor.generate(prompt="first", **common)
        with self.assertRaises(TaskConflict):
            executor.generate(prompt="changed", **common)

    def test_audio_cannot_be_the_only_reference(self) -> None:
        audio = self._artifact("excerpt.wav", "audio/wav")
        executor = VideoExecutor(self.artifacts, self.tasks)
        with self.assertRaisesRegex(ValueError, "at least one image or video"):
            executor.generate(project_id="promo-test", idempotency_key="x", model="minimax-h3-ref2va",
                              prompt="test", duration_seconds=6, aspect_ratio="9:16",
                              references={"images": [], "videos": [],
                                          "audios": [{"artifact_id": audio.artifact_id, "purpose": "rhythm"}]})


if __name__ == "__main__":
    unittest.main()
