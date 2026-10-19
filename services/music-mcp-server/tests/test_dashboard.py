from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import wave

from starlette.applications import Starlette
from starlette.testclient import TestClient

from verdantflare_music_mcp import __version__, server
from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.dashboard import MusicDashboard
from verdantflare_music_mcp.dashboard_assets import DashboardAssets, build as build_dashboard_assets
from verdantflare_music_mcp.executor import MusicExecutor, ServiceURLs
from verdantflare_music_mcp.registry import EtcdRegistry
from verdantflare_music_mcp.tasks import TaskStore


def _make_wav_bytes(duration_sec: float = 1.0, rate: int = 32000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00\x00\x00" * int(rate * duration_sec))
    return buf.getvalue()


class DashboardAndTasksTest(unittest.TestCase):
    def test_dashboard_assets_build_and_verify(self) -> None:
        source_dir = Path(server.__file__).resolve().parent / "dashboard_src"
        with tempfile.TemporaryDirectory() as out_dir:
            declaration = build_dashboard_assets(source_dir, out_dir, version=__version__)
            self.assertEqual(declaration["appId"], "music-mcp")
            self.assertEqual(declaration["appVersion"], __version__)
            self.assertEqual(declaration["dashboard"]["adapter"], "wujie")
            self.assertEqual(declaration["dashboard"]["appApiPolicyRef"], "music-mcp-dashboard-v1")

            assets = DashboardAssets(out_dir, version=__version__)
            html_bytes, mime = assets.asset("index.html")
            self.assertEqual(mime, "text/html; charset=utf-8")
            html_str = html_bytes.decode("utf-8")
            self.assertIn("Music MCP", html_str)
            self.assertIn(f"v{__version__}", html_str)
            self.assertNotIn("5090dev", html_str)
            self.assertNotIn("应用中心 /", html_str)
            self.assertNotIn("detailTriSteps", html_str)
            self.assertIn("1. 提示词与编曲指令", html_str)
            self.assertIn("2. 结构化歌词 / LRC 时间轴", html_str)
            self.assertIn("3. 参考音频 / 输入分轨干声", html_str)
            self.assertIn("4. 角色声线模型（RVC Voice Model）", html_str)
            self.assertIn('id="appHeader"', html_str)
            self.assertIn('id="appTabs"', html_str)
            self.assertNotIn("参考图像与封面", html_str)
            self.assertNotIn("detail-link", html_str)

            css_key = next(k for k in assets.files if k.endswith(".css"))
            css_bytes, _ = assets.asset(css_key)
            css_str = css_bytes.decode("utf-8")
            self.assertIn("--vf-shadow-card", css_str)
            self.assertIn("tabular-nums", css_str)
            self.assertIn("object-fit: contain", css_str)
            self.assertIn(".ref-empty-box", css_str)
            self.assertIn(".wave-bars", css_str)
            self.assertNotIn(".tri-steps", css_str)

            mjs_key = next(k for k in assets.files if k.endswith(".mjs"))
            mjs_bytes, _ = assets.asset(mjs_key)
            mjs_str = mjs_bytes.decode("utf-8")
            self.assertIn("Math.pow(rawNorm, 0.7)", mjs_str)
            self.assertNotIn("detail-link", mjs_str)
            self.assertNotIn(">查看<", mjs_str)
            self.assertNotIn("查看模型与渠道详情", mjs_str)
            self.assertNotIn("model-expand-trigger", mjs_str)
            self.assertNotIn("令牌直通", mjs_str)

    def test_task_store_and_dashboard_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            wav_payload = _make_wav_bytes(duration_sec=2.0, rate=40000)
            rec = store.create(
                project_id="mengsk-comfort",
                operation="voice.convert",
                filename="vocal_dry_cloned.wav",
                media_type="audio/wav",
                payload=wav_payload,
            )
            lrc_rec = store.create(
                project_id="mengsk-comfort",
                operation="lyrics.align",
                filename="aligned.lrc",
                media_type="text/plain; charset=utf-8",
                payload="[00:01.00] 夜雾漫过铁桥的边缘\n".encode("utf-8"),
            )

            tasks = TaskStore(store)
            synced = tasks.list_tasks()
            self.assertEqual(len(synced), 2)

            exec_inst = MusicExecutor(store, ServiceURLs(None, None, None, None, None, None))
            reg = EtcdRegistry(
                domain="music",
                tools=server.TOOLS_SCHEMA,
                version=__version__,
                etcd_endpoints="http://127.0.0.1:2379",
            )
            dash = MusicDashboard(
                store=store,
                tasks=tasks,
                executor=exec_inst,
                registry=reg,
                tools_schema=server.TOOLS_SCHEMA,
            )
            test_app = Starlette(routes=dash.routes())
            test_app.add_middleware(server.BearerAuthMiddleware, token="secret-token")

            with TestClient(test_app) as client:
                # 1. Internal declaration & index accessible without Bearer token (Studio RegisteredSource)
                decl_resp = client.get("/internal/dashboard-declaration")
                self.assertEqual(decl_resp.status_code, 200)
                self.assertEqual(decl_resp.json()["appId"], "music-mcp")

                idx_resp = client.get("/internal/dashboard-index")
                self.assertEqual(idx_resp.status_code, 200)
                self.assertIn("index.html", idx_resp.json()["files"])

                entry_resp = client.get("/internal/dashboard/index.html")
                self.assertEqual(entry_resp.status_code, 200)
                self.assertIn("Music MCP", entry_resp.text)

                # 2. Dashboard summary API
                dash_resp = client.get("/api/dashboard")
                self.assertEqual(dash_resp.status_code, 200)
                dash_json = dash_resp.json()
                self.assertEqual(dash_json["app_version"], __version__)
                self.assertEqual(len(dash_json["tasks"]), 2)

                task_id = dash_json["tasks"][0]["task_id"]

                # 3. Task detail & title rename persistence
                detail_resp = client.get(f"/api/tasks/{task_id}")
                self.assertEqual(detail_resp.status_code, 200)
                self.assertIn("夜雾漫过铁桥的边缘", detail_resp.json()["task"]["lyrics_lrc"] or "")

                rename_resp = client.post(
                    f"/api/tasks/{task_id}/title",
                    json={"title": "孟斯克声线翻唱验收版"},
                )
                self.assertEqual(rename_resp.status_code, 200)
                self.assertEqual(rename_resp.json()["task"]["title"], "孟斯克声线翻唱验收版")
                self.assertEqual(rename_resp.json()["task"]["title_revision"], 2)

                # Re-instantiate TaskStore to verify persistence on disk
                reloaded = TaskStore(store).get_task(task_id)
                self.assertEqual(reloaded["title"], "孟斯克声线翻唱验收版")

                # 4. Model catalog & MCP status
                cat_resp = client.get("/api/model-catalog")
                self.assertEqual(cat_resp.status_code, 200)
                cat_json = cat_resp.json()
                self.assertEqual(len(cat_json["models"]), 8)

                mcp_resp = client.get("/api/mcp/status")
                self.assertEqual(mcp_resp.status_code, 200)
                self.assertEqual(len(mcp_resp.json()["tools"]), 21)

                # 5. Audio preview & lossless chunk download
                prev_resp = client.get(f"/api/artifacts/{rec.artifact_id}/preview")
                self.assertEqual(prev_resp.status_code, 200)
                self.assertTrue(prev_resp.json()["data_url"].startswith("data:audio/"))

                chunk_resp = client.get(f"/api/artifacts/{rec.artifact_id}/chunk/0")
                self.assertEqual(chunk_resp.status_code, 200)
                raw_back = base64.b64decode(chunk_resp.json()["data_b64"])
                self.assertEqual(raw_back, wav_payload)


if __name__ == "__main__":
    unittest.main()
