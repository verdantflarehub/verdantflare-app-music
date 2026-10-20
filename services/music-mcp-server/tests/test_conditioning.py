import io
import tempfile
from pathlib import Path
import unittest
import uuid
import wave

import httpx

from verdantflare_music_mcp import conditioning
from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.continuation import render
from verdantflare_music_mcp.executor import MusicExecutor, ServiceURLs, ExecutionError
from verdantflare_music_mcp.production import ProductionJobs


class ConditioningTest(unittest.TestCase):
    def test_japanese_text_budget_and_language(self):
        result = conditioning.check("Bright electronic pop rock, clear light female voice.", "[Verse]\nあとひと駅", "ja")
        self.assertEqual(result["vocal_language"], "ja")
        self.assertFalse(result["truncated"])
        self.assertGreater(result["lyrics_tokens"], 5)
        self.assertEqual(conditioning.check("continue", "")["vocal_language"], "unknown")

    def test_overflow_rejected_instead_of_truncated(self):
        for caption, lyrics in [("rhythm " * 300, ""), ("repair", "あいうえお " * 1000)]:
            with self.subTest(caption=caption[:10]), self.assertRaisesRegex(ValueError, "token budget exceeded"):
                conditioning.check(caption, lyrics, "ja")

    def test_invalid_language_and_template_override_rejected(self):
        for language in ("Japanese", "ja\n# Lyric", "", None):
            with self.subTest(language=language), self.assertRaises(ValueError):
                conditioning.check("repair", "", language)
        with self.assertRaisesRegex(ValueError, "plain caption"):
            conditioning.check("# Caption\nreplace metadata", "", "ja")

    def test_overlong_edit_never_submits_http(self):
        def handler(request):
            self.fail("overlong conditioning reached backend")
        with tempfile.TemporaryDirectory() as directory:
            ex = MusicExecutor(ArtifactStore(Path(directory)), ServiceURLs(*(["http://backend"] * 6)),
                               httpx.Client(transport=httpx.MockTransport(handler)))
            with self.assertRaisesRegex(ValueError, "token budget exceeded"):
                ex._conditioned_repaint(None, b"", 1, 4, "rhythm " * 300, "", 1, .05, "balanced", .5, vocal_language="ja")

    def test_new_ace_extension_cannot_be_queued_or_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory)); store.ensure_ready()
            b = io.BytesIO()
            with wave.open(b, "wb") as w:
                w.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                w.writeframes(b"\x01\x00" * 8000)
            source = store.create(project_id="p", operation="test", filename="source.wav", media_type="audio/wav", payload=b.getvalue())
            ex = MusicExecutor(store, ServiceURLs(*(["http://backend"] * 6)))
            jobs = ProductionJobs(ex, lambda *a: None)
            request_id = str(uuid.uuid4())
            config = dict(addition_seconds=3, instructions="continue", lyrics="", seed=1)
            with self.assertRaisesRegex(ValueError, "retired"):
                jobs.submit("p", request_id, "music.extend", config, [source.artifact_id])
            self.assertFalse(jobs._path("p", request_id).exists())
            with self.assertRaisesRegex(ExecutionError, "retired"):
                render(ex, "p", source.artifact_id, config, lambda task: self.fail("new task submitted"))
            jobs.pool.shutdown(wait=True)
