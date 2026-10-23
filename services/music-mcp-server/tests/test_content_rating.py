from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

from starlette.testclient import TestClient

import verdantflare_music_mcp.server as server_mod
from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.content_rating import ContentRatingError, ContentRatingStore
from verdantflare_music_mcp.tasks import TaskNotFound, TaskStore


def _wav_bytes(duration_sec: float = 1.0, rate: int = 32000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00\x00\x00" * int(rate * duration_sec))
    return buf.getvalue()


class TestContentRatingStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.store = ContentRatingStore(self.db_path, domain="music")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_only_and_human_override_lifecycle(self) -> None:
        # 1. Rate general
        r1 = self.store.rate_content(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            rating="general",
            reason="歌词与人声均为正常抒情流行曲目，未触发类别",
            rubric_version="rubric v1.0",
        )
        self.assertEqual(r1["rating"], "general")
        self.assertFalse(r1["ignored"])

        # 2. Upgrade general -> nsfw (lyrics evaluation rationale)
        r2 = self.store.rate_content(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            rating="nsfw",
            reasons=["S1：副歌歌词含露骨性兴奋描写", "S2：主歌歌词含明确性行为描写"],
            rubric_version="rubric v1.0",
            evaluator_model="claude-sonnet-4-6",
        )
        self.assertEqual(r2["rating"], "nsfw")
        self.assertFalse(r2["ignored"])
        self.assertTrue(self.store.is_nsfw("art_11111111111111111111111111111111"))

        # 3. Attempt to downgrade nsfw -> general via MCP (must be ignored)
        r3 = self.store.rate_content(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            rating="general",
            reason="attempt to clear via mcp",
            rubric_version="rubric v1.0",
        )
        self.assertEqual(r3["rating"], "nsfw")
        self.assertTrue(r3["ignored"])
        self.assertTrue(self.store.is_nsfw("art_11111111111111111111111111111111"))

        # 4. Clear by human
        r4 = self.store.clear_rating(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            actor_user_id="user-admin",
            note="歌词为隐喻爱情表达，人工复核属于误标",
        )
        self.assertEqual(r4["rating"], "general")
        self.assertEqual(r4["source"], "human")
        self.assertTrue(r4["human_cleared"])
        self.assertFalse(self.store.is_nsfw("art_11111111111111111111111111111111"))

        # 4b. AI attempts to re-rate nsfw after human clear -> ignored!
        r4b = self.store.rate_content(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            rating="nsfw",
            reason="ai re-rate attempt on lyrics",
            rubric_version="rubric v1.0",
        )
        self.assertEqual(r4b["rating"], "general")
        self.assertTrue(r4b["ignored"])
        self.assertFalse(self.store.is_nsfw("art_11111111111111111111111111111111"))

        # 5. Mark by human
        r5 = self.store.mark_rating(
            version_id="art_11111111111111111111111111111111",
            project_id="proj-music-1",
            actor_user_id="user-admin",
            reason="manual 18+ lyrics mark",
        )
        self.assertEqual(r5["rating"], "nsfw")
        self.assertEqual(r5["source"], "human")
        self.assertTrue(self.store.is_nsfw("art_11111111111111111111111111111111"))

        events = self.store.list_events("art_11111111111111111111111111111111")
        self.assertEqual(
            [e["event_type"] for e in events],
            ["rate", "rate", "rate", "clear", "ignored_after_clear", "rate"],
        )

    def test_validation_rejects_invalid_rating_or_reason(self) -> None:
        with self.assertRaises(ContentRatingError) as ctx_clear:
            self.store.rate_content(
                version_id="art_11111111111111111111111111111111",
                project_id="proj-music-1",
                rating="unrated",
                reason="try clear",
            )
        self.assertEqual(ctx_clear.exception.error_code, "RATING_CLEAR_FORBIDDEN")

        with self.assertRaises(ContentRatingError):
            self.store.rate_content(
                version_id="art_11111111111111111111111111111111",
                project_id="proj-music-1",
                rating="r18",
                reason="bad enum",
            )
        with self.assertRaises(ContentRatingError):
            self.store.rate_content(
                version_id="art_11111111111111111111111111111111",
                project_id="proj-music-1",
                rating="nsfw",
                reason="   ",
            )


class TestMusicMcpAndHttpContentRating(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.prev_token = os.environ.get("MUSIC_MCP_BEARER_TOKEN")
        os.environ["MUSIC_MCP_BEARER_TOKEN"] = "test-secret-token"

        self.orig_store = server_mod.store
        self.orig_artifacts = server_mod.artifacts
        self.orig_task_store = server_mod.task_store
        self.orig_tasks = server_mod.tasks
        self.orig_ratings = server_mod.ratings

        self.artifacts = ArtifactStore(root)
        self.ratings = ContentRatingStore(root / "tasks.db", domain="music")
        self.tasks = TaskStore(self.artifacts, ratings=self.ratings)
        self.tasks.ensure_ready()

        server_mod.store = self.artifacts
        server_mod.artifacts = self.artifacts
        server_mod.task_store = self.tasks
        server_mod.tasks = self.tasks
        server_mod.ratings = self.ratings
        server_mod.app.state.store = self.artifacts
        server_mod.app.state.artifacts = self.artifacts
        server_mod.app.state.tasks = self.tasks
        server_mod.app.state.task_store = self.tasks
        server_mod.app.state.ratings = self.ratings

        # Seed 1 general song task and 1 explicit-lyrics song task
        self.art_gen = self.artifacts.create(
            project_id="proj-music-a",
            operation="music.generate",
            filename="sunny_ballad.wav",
            media_type="audio/wav",
            payload=_wav_bytes(1.0),
        )
        self.task_gen = self.tasks.record_operation(
            project_id="proj-music-a",
            operation="music.generate",
            records=[self.art_gen],
            params={
                "lyrics": "[Verse]\n晨光穿过树梢，微风吹过山岗\n",
                "instructions": "warm acoustic ballad",
            },
        )

        self.art_nsfw = self.artifacts.create(
            project_id="proj-music-a",
            operation="music.generate",
            filename="explicit_club_demo.wav",
            media_type="audio/wav",
            payload=_wav_bytes(1.5),
        )
        self.task_nsfw = self.tasks.record_operation(
            project_id="proj-music-a",
            operation="music.generate",
            records=[self.art_nsfw],
            params={
                "lyrics": "[Verse]\n成人向测试曲目歌词\n",
                "instructions": "late night club track",
            },
        )

    def tearDown(self) -> None:
        if self.prev_token is None:
            os.environ.pop("MUSIC_MCP_BEARER_TOKEN", None)
        else:
            os.environ["MUSIC_MCP_BEARER_TOKEN"] = self.prev_token
        server_mod.store = self.orig_store
        server_mod.artifacts = self.orig_artifacts
        server_mod.task_store = self.orig_task_store
        server_mod.tasks = self.orig_tasks
        server_mod.ratings = self.orig_ratings
        server_mod.app.state.store = self.orig_store
        server_mod.app.state.artifacts = self.orig_artifacts
        server_mod.app.state.tasks = self.orig_tasks
        server_mod.app.state.task_store = self.orig_task_store
        server_mod.app.state.ratings = self.orig_ratings
        self._tmp.cleanup()

    def test_mcp_lyrics_nsfw_rating_hides_from_mcp_and_http_supports_local_switch_and_clear(self) -> None:
        # Initially both tasks are visible in MCP list
        initial_list = json.loads(server_mod.music_list(project_id="proj-music-a").content[0].text)
        self.assertEqual(initial_list["total"], 2)

        # Rate explicit-lyrics track as nsfw via MCP tool
        rate_res = json.loads(
            server_mod.music_rate_content(
                version_id=self.art_nsfw.artifact_id,
                rating="nsfw",
                reasons=["S1/S2：歌词含露骨性行为描写，转写文本与提交歌词一致"],
                rubric_version="rubric v1.0",
                evaluator_model="claude-sonnet-4-6",
            ).content[0].text
        )
        self.assertEqual(rate_res["rating"], "nsfw")
        self.assertFalse(rate_res["ignored"])
        self.assertIn("歌词含露骨性行为描写", rate_res["reason"])

        # Attempting to downgrade via MCP general must not clear nsfw
        downgrade_res = json.loads(
            server_mod.music_rate_content(
                version_id=self.art_nsfw.artifact_id,
                rating="general",
                reason="try downgrade via mcp",
                rubric_version="rubric v1.0",
            ).content[0].text
        )
        self.assertEqual(downgrade_res["rating"], "nsfw")
        self.assertTrue(downgrade_res["ignored"])

        # MCP list now hides the nsfw task and decrements total (no placeholder)
        after_list = json.loads(server_mod.music_list(project_id="proj-music-a").content[0].text)
        self.assertEqual(after_list["total"], 1)
        self.assertEqual([t["task_id"] for t in after_list["tasks"]], [self.task_gen["task_id"]])

        # MCP status and result return NOT_FOUND for the nsfw task
        with self.assertRaises(TaskNotFound):
            server_mod.music_status(self.task_nsfw["task_id"])
        with self.assertRaises(TaskNotFound):
            server_mod.music_result(self.task_nsfw["task_id"])

        # Unknown or cross-project version_id raises TaskNotFound
        with self.assertRaises(TaskNotFound):
            server_mod.music_rate_content(
                version_id="art_00000000000000000000000000000000",
                rating="general",
                reason="missing",
            )
        with self.assertRaises(TaskNotFound):
            server_mod.music_rate_content(
                version_id=self.art_nsfw.artifact_id,
                rating="general",
                reason="wrong project",
                project_id="proj-other",
            )

        headers = {"Authorization": "Bearer test-secret-token"}
        client = TestClient(server_mod.app)

        # HTTP /api/tasks and /api/tasks/stats without X-VF-Include-NSFW hide the nsfw task
        resp_default = client.get("/api/tasks?project_id=proj-music-a", headers=headers)
        self.assertEqual(resp_default.status_code, 200)
        self.assertEqual(resp_default.json()["total"], 1)

        resp_stats_default = client.get("/music/api/tasks/stats?project_id=proj-music-a", headers=headers)
        self.assertEqual(resp_stats_default.status_code, 200)
        self.assertEqual(resp_stats_default.json()["total"], 1)

        # HTTP /artifacts/{id}/content and /api/artifacts/{id}/preview without X-VF-Include-NSFW return 404
        resp_art_hidden = client.get(f"/artifacts/{self.art_nsfw.artifact_id}/content", headers=headers)
        self.assertEqual(resp_art_hidden.status_code, 404)
        resp_prev_hidden = client.get(f"/api/artifacts/{self.art_nsfw.artifact_id}/preview", headers=headers)
        self.assertEqual(resp_prev_hidden.status_code, 404)

        # HTTP /api/tasks with X-VF-Include-NSFW: 1 includes the nsfw task with UI fields
        resp_inc = client.get(
            "/music/api/tasks?project_id=proj-music-a",
            headers={**headers, "X-VF-Include-NSFW": "1"},
        )
        self.assertEqual(resp_inc.status_code, 200)
        inc_data = resp_inc.json()
        self.assertEqual(inc_data["total"], 2)
        nsfw_item = next(t for t in inc_data["tasks"] if t["task_id"] == self.task_nsfw["task_id"])
        self.assertEqual(nsfw_item["content_rating"]["rating"], "nsfw")
        self.assertTrue(nsfw_item["can_clear_rating"])
        self.assertFalse(nsfw_item["can_mark_rating"])

        # HTTP /artifacts/{id}/content with X-VF-Include-NSFW: 1 returns 200
        resp_art_shown = client.get(
            f"/music/artifacts/{self.art_nsfw.artifact_id}/content",
            headers={**headers, "X-VF-Include-NSFW": "1"},
        )
        self.assertEqual(resp_art_shown.status_code, 200)

        # Internal clear endpoint requires Bearer auth
        resp_unauth = client.post(
            f"/internal/content-ratings/{self.art_nsfw.artifact_id}/clear",
            json={"note": "unauthorized attempt"},
        )
        self.assertEqual(resp_unauth.status_code, 401)

        # Internal human clear endpoint restores general visibility
        resp_clear = client.post(
            f"/internal/content-ratings/{self.art_nsfw.artifact_id}/clear",
            json={"note": "歌词复核属实误标，人工撤销"},
            headers={**headers, "X-User-Id": "user-mengsk"},
        )
        self.assertEqual(resp_clear.status_code, 200)
        self.assertEqual(resp_clear.json()["rating"], "general")
        self.assertEqual(resp_clear.json()["source"], "human")
        self.assertTrue(resp_clear.json()["human_cleared"])

        # Now MCP list and status see both tasks again
        restored_list = json.loads(server_mod.music_list(project_id="proj-music-a").content[0].text)
        self.assertEqual(restored_list["total"], 2)
        status_res = json.loads(server_mod.music_status(self.task_nsfw["task_id"]).content[0].text)
        self.assertEqual(status_res["status"], "completed")

        # Internal human mark endpoint marks general -> nsfw
        resp_mark = client.post(
            f"/music/internal/content-ratings/{self.art_nsfw.artifact_id}/mark",
            json={"reason": "人工复核确认为 18+ 歌词"},
            headers={**headers, "X-User-Id": "user-mengsk"},
        )
        self.assertEqual(resp_mark.status_code, 200)
        self.assertEqual(resp_mark.json()["rating"], "nsfw")
        self.assertEqual(resp_mark.json()["source"], "human")
        self.assertEqual(
            json.loads(server_mod.music_list(project_id="proj-music-a").content[0].text)["total"],
            1,
        )

    def test_mixed_multi_artifact_task_filters_only_nsfw_artifact(self) -> None:
        inst_art = self.artifacts.create(
            project_id="proj-music-a",
            operation="stems.separate",
            filename="instrumental.wav",
            media_type="audio/wav",
            payload=_wav_bytes(1.0),
        )
        vocal_art = self.artifacts.create(
            project_id="proj-music-a",
            operation="stems.separate",
            filename="vocal_dry.wav",
            media_type="audio/wav",
            payload=_wav_bytes(1.0),
        )
        mixed_task = self.tasks.record_operation(
            project_id="proj-music-a",
            operation="stems.separate",
            records=[inst_art, vocal_art],
            params={"audio_asset_id": self.art_gen.artifact_id},
        )

        # Rate only the vocal stem as nsfw
        server_mod.music_rate_content(
            version_id=vocal_art.artifact_id,
            rating="nsfw",
            reason="S1：人声干声歌词含露骨成人描写，伴奏轨无歌词",
            rubric_version="rubric v1.0",
        )

        # Mixed task remains visible, but only returns the non-NSFW instrumental artifact
        res = json.loads(server_mod.music_result(mixed_task["task_id"]).content[0].text)
        self.assertEqual([a["artifact_id"] for a in res["artifacts"]], [inst_art.artifact_id])

        # Once the remaining artifact is also rated nsfw, the entire task returns NOT_FOUND
        server_mod.music_rate_content(
            version_id=inst_art.artifact_id,
            rating="nsfw",
            reason="S1：整曲归档隐藏",
            rubric_version="rubric v1.0",
        )
        with self.assertRaises(TaskNotFound):
            server_mod.music_result(mixed_task["task_id"])


if __name__ == "__main__":
    unittest.main()
