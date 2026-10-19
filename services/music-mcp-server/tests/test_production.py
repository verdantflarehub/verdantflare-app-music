import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import zipfile

import httpx
from verdantflare_music_mcp.artifacts import ArtifactStore, ArtifactNotFound
from verdantflare_music_mcp.executor import MusicExecutor, ServiceURLs
from verdantflare_music_mcp.production import ProductionJobs


class ProductionJobsTest(unittest.TestCase):
    def test_idempotency_recovery_and_cross_project_denial(self):
        calls = []
        payload = b"wave-fixture"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("render.wav", payload)
            z.writestr(
                "report.json",
                json.dumps(
                    {
                        "outputs_sha256": {
                            "render.wav": hashlib.sha256(payload).hexdigest()
                        }
                    }
                ),
            )

        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=out.getvalue())

        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d))
            store.ensure_ready()
            ex = MusicExecutor(
                store,
                ServiceURLs(*(["http://backend"] * 5)),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            jobs = ProductionJobs(ex, lambda *args: None)
            source = store.create(
                project_id="p",
                operation="test",
                filename="source.wav",
                media_type="audio/wav",
                payload=b"source",
            )
            request = str(uuid.uuid4())
            jobs.submit("p", request, "voice.tune", {}, [source.artifact_id])
            jobs.pool.shutdown(wait=True)
            result = jobs.status("p", request)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(len(result["artifacts"]), 2)
            again = jobs.submit("p", request, "voice.tune", {}, [source.artifact_id])
            self.assertEqual(result, again)
            self.assertEqual(len(calls), 1)
            with self.assertRaises(ValueError):
                jobs.submit(
                    "p", request, "voice.tune", {"strength": 1}, [source.artifact_id]
                )
            with self.assertRaises(ArtifactNotFound):
                jobs.submit(
                    "other", str(uuid.uuid4()), "voice.tune", {}, [source.artifact_id]
                )
            with self.assertRaises(ValueError):
                jobs.status("other", request)

    def test_cancel_queued_does_not_publish(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d))
            store.ensure_ready()
            jobs = ProductionJobs(
                MusicExecutor(store, ServiceURLs(*(["http://backend"] * 5))),
                lambda *args: None,
            )
            request = str(uuid.uuid4())
            path = jobs._path("p", request)
            jobs._save(path, {"epoch": jobs.epoch, "state": "queued", "artifacts": []})
            self.assertEqual(
                jobs.status("p", request, cancel=True)["state"], "cancelled"
            )
            jobs._run(path)
            self.assertEqual(jobs.status("p", request)["artifacts"], [])
            jobs.pool.shutdown()
