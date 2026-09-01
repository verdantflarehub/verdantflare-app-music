import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcp import types
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.results import artifact_result
from verdantflare_music_mcp import server
from verdantflare_music_mcp.server import (
    BearerAuthMiddleware,
    artifact_content,
    mcp,
    transport_security_from_environment,
)


class ServerTest(unittest.TestCase):
    def test_transport_security_defaults_to_loopback(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = transport_security_from_environment()

        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertEqual(settings.allowed_hosts, ["127.0.0.1:*", "localhost:*", "[::1]:*"])
        self.assertEqual(
            settings.allowed_origins,
            ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )

    def test_transport_security_uses_explicit_public_allowlist(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "MUSIC_MCP_ALLOWED_HOSTS": "mcp.example.com, 127.0.0.1:*",
                "MUSIC_MCP_ALLOWED_ORIGINS": "https://mcp.example.com",
            },
            clear=True,
        ):
            settings = transport_security_from_environment()

        self.assertEqual(settings.allowed_hosts, ["mcp.example.com", "127.0.0.1:*"])
        self.assertEqual(settings.allowed_origins, ["https://mcp.example.com"])

        middleware = TransportSecurityMiddleware(settings)

        async def validate(host: str):
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/mcp",
                    "headers": [
                        (b"host", host.encode()),
                        (b"content-type", b"application/json"),
                    ],
                }
            )
            return await middleware.validate_request(request, is_post=True)

        self.assertIsNone(asyncio.run(validate("mcp.example.com")))
        self.assertEqual(asyncio.run(validate("untrusted.example.com")).status_code, 421)

    def test_tools_expose_executable_contract(self) -> None:
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "asset.import",
                "music.generate",
                "stems.separate",
                "voice.prepare",
                "voice.train",
                "voice.convert",
                "lyrics.align",
                "mix.master",
            },
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
                "voice.prepare": {"project_id", "audio_asset_ids"},
                "voice.train": {"project_id", "audio_asset_id", "model_id", "epochs", "batch_size"},
                "voice.convert": {
                    "project_id",
                    "audio_asset_id",
                    "model_id",
                    "pitch_shift",
                    "f0_method",
                    "index_rate",
                    "filter_radius",
                    "rms_mix_rate",
                    "protect",
                },
                "lyrics.align": {"project_id", "vocal_asset_id", "lyrics", "language"},
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
