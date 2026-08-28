import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import types
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.results import artifact_result
from verdantflare_music_mcp import server
from verdantflare_music_mcp.server import BearerAuthMiddleware, artifact_content, mcp


class ServerTest(unittest.TestCase):
    def test_tools_expose_executable_contract(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {"asset.import", "music.generate", "stems.separate", "voice.train", "voice.convert", "mix.master"},
        )
        properties = {tool.name: set(tool.input_schema["properties"]) for tool in tools}
        self.assertEqual(
            properties,
            {
                "asset.import": {"project_id", "source_url", "filename", "expected_sha256"},
                "music.generate": {
                    "project_id",
                    "lyrics",
                    "instructions",
                    "candidate_number",
                    "seed",
                    "max_duration_seconds",
                },
                "stems.separate": {"project_id", "audio_asset_id"},
                "voice.train": {"project_id", "audio_asset_id", "model_id", "epochs", "batch_size"},
                "voice.convert": {"project_id", "audio_asset_id", "model_id", "pitch_shift"},
                "mix.master": {
                    "project_id",
                    "instrumental_asset_id",
                    "vocal_asset_id",
                    "lyrics_lrc",
                    "bpm",
                },
            },
        )

    def test_result_contains_artifacts_without_internal_service_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            record = store.create(
                project_id="mengsk-error",
                operation="music.generate",
                filename="candidate.wav",
                media_type="audio/wav",
                payload=b"audio",
            )
            result = artifact_result(store, "mengsk-error", "music.generate", [record])
            self.assertIsInstance(result.content[1], types.ResourceLink)
            serialized = json.dumps(result.structured_content)
            self.assertNotIn("music-minimax-music3-api", serialized)
            self.assertIn(record.artifact_id, serialized)

    def test_tool_call_returns_persisted_resource_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            record = store.create(
                project_id="mengsk-error",
                operation="music.generate",
                filename="Demo_Candidate_1.wav",
                media_type="audio/wav",
                payload=b"audio",
            )
            with (
                mock.patch.object(server, "store", store),
                mock.patch.object(server.executor, "generate", return_value=[record]),
            ):
                result = asyncio.run(
                    mcp.call_tool(
                        "music.generate",
                        {
                            "project_id": "mengsk-error",
                            "lyrics": "lyrics",
                            "instructions": "instructions",
                            "candidate_number": 1,
                            "seed": 7,
                            "max_duration_seconds": 90,
                        },
                    )
                )
            self.assertEqual(result.structured_content["artifacts"][0]["artifact_id"], record.artifact_id)
            self.assertTrue(any(isinstance(item, types.ResourceLink) for item in result.content))

    def test_bearer_auth_and_artifact_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            record = store.create(
                project_id="mengsk-error",
                operation="music.generate",
                filename="candidate.wav",
                media_type="audio/wav",
                payload=b"audio",
            )
            test_app = Starlette(routes=[Route("/artifacts/{artifact_id:str}/content", artifact_content)])
            test_app.add_middleware(BearerAuthMiddleware, token="secret-token")
            with mock.patch.object(server, "store", store), TestClient(test_app) as client:
                path = f"/artifacts/{record.artifact_id}/content"
                self.assertEqual(client.get(path).status_code, 401)
                response = client.get(path, headers={"Authorization": "Bearer secret-token"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"audio")
                self.assertEqual(response.headers["content-type"], "audio/wav")


if __name__ == "__main__":
    unittest.main()
