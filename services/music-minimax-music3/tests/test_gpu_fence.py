from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from guard.gpu_guard import FencePersistenceError, LeaseFenceGuard
from guard.proxy import create_music3_guard_app


class Music3GuardProxyTest(unittest.TestCase):
    def test_two_card_six_tuple_guard_and_durable_token_sealing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "music3_guard.json"
            ident = {
                "pod_uid": "pod-music3-1",
                "container_id": "containerd://cid-music3-1",
                "boot_id": "boot-music3-1",
                "supervisor_pid": 3100,
                "supervisor_starttime_ticks": 61000,
                "visible_gpu_uuids": [
                    "GPU-00000000-0000-0000-0000-000000000001",
                    "GPU-00000000-0000-0000-0000-000000000002",
                ],
            }

            def upstream_handler(req: httpx.Request) -> httpx.Response:
                if req.url.path == "/health":
                    return httpx.Response(200, json={"model": "MiniMax-Music3"})
                if req.url.path == "/v1/audio/speech":
                    return httpx.Response(200, content=b"RIFF-FAKE-WAV", headers={"content-type": "audio/wav"})
                return httpx.Response(404)

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-music3-secret",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch("guard.proxy.read_runtime_identity", return_value=ident),
                patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
            ):
                app = create_music3_guard_app(
                    upstream_url="http://127.0.0.1:18000",
                    state_path=state_path,
                    transport=httpx.MockTransport(upstream_handler),
                )
                client = TestClient(app)

                h = client.get("/health")
                self.assertEqual(h.status_code, 200)
                self.assertEqual(h.json()["runtime_identity"]["pod_uid"], "pod-music3-1")

                # Single-card grant rejected on 2-card Music3!
                one_card_hdrs = {
                    "X-VF-GPU-Lease-ID": "lease-m3-1",
                    "X-VF-GPU-Fencing-Token": "30",
                    "X-VF-GPU-Attempt-ID": "att-1",
                    "X-VF-GPU-Stage-ID": "music3_generate",
                    "X-VF-GPU-Parent-Task-ID": "task-m3-1",
                    "X-VF-GPU-Pod-UID": "pod-music3-1",
                    "X-VF-GPU-Container-ID": "cid-music3-1",
                    "X-VF-GPU-Boot-ID": "boot-music3-1",
                    "X-VF-GPU-UUID": "GPU-00000000-0000-0000-0000-000000000001",
                    "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                }
                r_1card = client.post("/v1/audio/speech", headers=one_card_hdrs, json={"input": "hi"})
                self.assertEqual(r_1card.status_code, 403)

                # Valid 2-card grant -> 200 + receiver_guard proof
                two_card_hdrs = dict(one_card_hdrs)
                two_card_hdrs["X-VF-GPU-UUID"] = (
                    "GPU-00000000-0000-0000-0000-000000000001,GPU-00000000-0000-0000-0000-000000000002"
                )
                r_ok = client.post("/v1/audio/speech", headers=two_card_hdrs, json={"input": "hi"})
                self.assertEqual(r_ok.status_code, 200)
                proof = json.loads(r_ok.headers["x-vf-reclaim-proof"])
                self.assertTrue(proof["reclaimed"])
                self.assertEqual(proof["proof_source"], "receiver_guard")
                self.assertTrue(proof["token_sealed"])
                self.assertEqual(proof["consumed_fencing_token"], 30)

                # Restart proxy on same state_path -> token 30 replay rejected 409!
                app2 = create_music3_guard_app(
                    upstream_url="http://127.0.0.1:18000",
                    state_path=state_path,
                    transport=httpx.MockTransport(upstream_handler),
                )
                client2 = TestClient(app2)
                replay_hdrs = dict(two_card_hdrs)
                replay_hdrs["X-VF-GPU-Attempt-ID"] = "att-2"
                r_replay = client2.post("/v1/audio/speech", headers=replay_hdrs, json={"input": "hi"})
                self.assertEqual(r_replay.status_code, 409)

    def test_upstream_timeout_holds_uncertain_until_sgl_omni_restarts_and_attests_resident_procs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "music3_uncertain.json"
            ident = {
                "pod_uid": "pod-music3-2",
                "container_id": "containerd://cid-music3-2",
                "boot_id": "boot-music3-2",
                "supervisor_pid": 3200,
                "supervisor_starttime_ticks": 62000,
                "visible_gpu_uuids": [
                    "GPU-00000000-0000-0000-0000-000000000001",
                    "GPU-00000000-0000-0000-0000-000000000002",
                ],
            }
            upstream_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                mode = {"behavior": "timeout"}

                def upstream_handler(req: httpx.Request) -> httpx.Response:
                    if req.url.path == "/health":
                        return httpx.Response(200, json={"model": "MiniMax-Music3"})
                    if req.url.path == "/v1/audio/speech":
                        if mode["behavior"] == "timeout":
                            raise httpx.ReadTimeout("sgl-omni still generating on 2 GPUs")
                        return httpx.Response(200, content=b"RIFF-FAKE-WAV", headers={"content-type": "audio/wav"})
                    return httpx.Response(404)

                env = {
                    "MUSIC_GPU_GRANT_POLICY": "require",
                    "STATION_GPU_SCHEDULER_TOKEN": "sched-music3-secret",
                }
                hdrs = {
                    "X-VF-GPU-Lease-ID": "lease-m3-u1",
                    "X-VF-GPU-Fencing-Token": "71",
                    "X-VF-GPU-Attempt-ID": "att-u1",
                    "X-VF-GPU-Stage-ID": "music3_generate",
                    "X-VF-GPU-Parent-Task-ID": "task-m3-u1",
                    "X-VF-GPU-Pod-UID": "pod-music3-2",
                    "X-VF-GPU-Container-ID": "cid-music3-2",
                    "X-VF-GPU-Boot-ID": "boot-music3-2",
                    "X-VF-GPU-UUID": "GPU-00000000-0000-0000-0000-000000000001,GPU-00000000-0000-0000-0000-000000000002",
                    "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                }
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch("guard.proxy.read_runtime_identity", return_value=ident),
                    patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
                ):
                    app1 = create_music3_guard_app(
                        upstream_url="http://127.0.0.1:18000",
                        state_path=state_path,
                        upstream_leader_pid=upstream_proc.pid,
                        transport=httpx.MockTransport(upstream_handler),
                    )
                    c1 = TestClient(app1)
                    self.assertEqual(c1.post("/v1/audio/speech", headers=hdrs, json={"input": "song"}).status_code, 504)

                    # Across proxy restart while same sgl-omni PID is alive -> /reclaim MUST return 409!
                    app2 = create_music3_guard_app(
                        upstream_url="http://127.0.0.1:18000",
                        state_path=state_path,
                        upstream_leader_pid=upstream_proc.pid,
                        transport=httpx.MockTransport(upstream_handler),
                    )
                    c2 = TestClient(app2)
                    r_busy = c2.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-music3-secret"},
                        json={"lease_id": "lease-m3-u1", "fencing_token": 71},
                    )
                    self.assertEqual(r_busy.status_code, 409)
                    self.assertFalse(r_busy.json()["reclaim_proof"]["reclaimed"])

                    # Kill sgl-omni -> dead upstream reports 503 on /health and 409 on /reclaim
                    upstream_proc.terminate()
                    upstream_proc.wait(timeout=5.0)
                    self.assertEqual(c2.get("/health").status_code, 503)
                    r_dead = c2.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-music3-secret"},
                        json={"lease_id": "lease-m3-u1", "fencing_token": 71},
                    )
                    self.assertEqual(r_dead.status_code, 409)
                    self.assertFalse(r_dead.json()["reclaim_proof"]["upstream_alive"])

                # Start fresh sgl-omni + synthetic /proc TP=2 worker tree (init=1, leader, tp0, tp1)
                upstream_proc2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                try:
                    proc_root = root / "fake_proc"

                    def write_stat(pid: int, ppid: int, ticks: int) -> None:
                        pdir = proc_root / str(pid)
                        pdir.mkdir(parents=True, exist_ok=True)
                        fields = ["S", str(ppid)] + ["0"] * 17 + [str(ticks)] + ["0"] * 5
                        (pdir / "stat").write_text(f"{pid} (sgl-omni) " + " ".join(fields), encoding="utf-8")

                    write_stat(1, 0, 100)
                    write_stat(3200, 1, 62000)
                    write_stat(upstream_proc2.pid, 1, 90100)
                    write_stat(9101, upstream_proc2.pid, 90201)
                    write_stat(9102, upstream_proc2.pid, 90202)

                    mode["behavior"] = "ok"
                    with (
                        patch.dict(os.environ, env, clear=False),
                        patch("guard.proxy.read_runtime_identity", return_value=ident),
                        patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
                    ):
                        app3 = create_music3_guard_app(
                            upstream_url="http://127.0.0.1:18000",
                            state_path=state_path,
                            init_pid=1,
                            upstream_leader_pid=upstream_proc2.pid,
                            proc_root=proc_root,
                            transport=httpx.MockTransport(upstream_handler),
                        )
                        c3 = TestClient(app3)
                        r_reclaim_ok = c3.post(
                            "/internal/v1/gpu-guard/reclaim",
                            headers={"Authorization": "Bearer sched-music3-secret"},
                            json={"lease_id": "lease-m3-u2", "fencing_token": 75},
                        )
                        self.assertEqual(r_reclaim_ok.status_code, 200)
                        proof = r_reclaim_ok.json()["reclaim_proof"]
                        self.assertTrue(proof["reclaimed"])
                        self.assertEqual(
                            proof["resident_procs"],
                            sorted(
                                [
                                    {"pid": 1, "starttime_ticks": 100},
                                    {"pid": upstream_proc2.pid, "starttime_ticks": 90100},
                                    {"pid": 9101, "starttime_ticks": 90201},
                                    {"pid": 9102, "starttime_ticks": 90202},
                                ],
                                key=lambda x: x["pid"],
                            ),
                        )

                        # Corrupt state file -> 503 fail closed
                        corrupt_path = root / "m3_corrupt.json"
                        corrupt_path.write_text("{corrupt", encoding="utf-8")
                        app_bad = create_music3_guard_app(
                            upstream_url="http://127.0.0.1:18000",
                            state_path=corrupt_path,
                            transport=httpx.MockTransport(upstream_handler),
                        )
                        c_bad = TestClient(app_bad)
                        hdrs2 = dict(hdrs)
                        hdrs2["X-VF-GPU-Fencing-Token"] = "80"
                        self.assertEqual(c_bad.post("/v1/audio/speech", headers=hdrs2, json={"input": "x"}).status_code, 503)
                        g_enospc = LeaseFenceGuard(
                            state_path=root / "enospc.json",
                            write_fn=lambda _fd, _b: (_ for _ in ()).throw(OSError(28, "ENOSPC")),
                        )
                        with self.assertRaises(FencePersistenceError):
                            g_enospc.reclaim_and_seal(lease_id="l-enospc", fencing_token=99, identity=ident)
                finally:
                    upstream_proc2.terminate()
                    upstream_proc2.wait(timeout=5.0)
            finally:
                if upstream_proc.poll() is None:
                    upstream_proc.terminate()
                    upstream_proc.wait(timeout=5.0)

    def test_same_attempt_stage_inflight_and_lost_response_replay_never_double_invokes_sgl_omni(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "m3_dedup.json"
            ident = {
                "pod_uid": "pod-music3-dedup",
                "container_id": "containerd://cid-music3-dedup",
                "boot_id": "boot-music3-dedup",
                "supervisor_pid": 3300,
                "supervisor_starttime_ticks": 63000,
                "visible_gpu_uuids": [
                    "GPU-00000000-0000-0000-0000-000000000001",
                    "GPU-00000000-0000-0000-0000-000000000002",
                ],
            }
            entered_upstream = threading.Event()
            release_upstream = threading.Event()
            upstream_calls = {"n": 0}

            class BlockingAsyncTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
                    if req.url.path == "/v1/audio/speech":
                        upstream_calls["n"] += 1
                        entered_upstream.set()
                        await __import__("anyio").to_thread.run_sync(release_upstream.wait, 5.0)
                        return httpx.Response(200, content=b"RIFF-DEDUP-WAV", headers={"content-type": "audio/wav"})
                    return httpx.Response(200, json={"model": "MiniMax-Music3"})

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-music3-secret",
            }
            hdrs = {
                "X-VF-GPU-Lease-ID": "lease-m3-dedup-1",
                "X-VF-GPU-Fencing-Token": "95",
                "X-VF-GPU-Attempt-ID": "att-m3-dedup-1",
                "X-VF-GPU-Stage-ID": "music3_generate",
                "X-VF-GPU-Parent-Task-ID": "task-m3-dedup-1",
                "X-VF-GPU-Pod-UID": "pod-music3-dedup",
                "X-VF-GPU-Container-ID": "cid-music3-dedup",
                "X-VF-GPU-Boot-ID": "boot-music3-dedup",
                "X-VF-GPU-UUID": "GPU-00000000-0000-0000-0000-000000000001,GPU-00000000-0000-0000-0000-000000000002",
                "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch("guard.proxy.read_runtime_identity", return_value=ident),
                patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
            ):
                app = create_music3_guard_app(
                    upstream_url="http://127.0.0.1:18000",
                    state_path=state_path,
                    transport=BlockingAsyncTransport(),
                )
                with TestClient(app) as client:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        fut1 = pool.submit(client.post, "/v1/audio/speech", headers=hdrs, json={"input": "chorus"})
                        self.assertTrue(entered_upstream.wait(timeout=5.0))

                        # Second concurrent request with identical (parent_task_id, attempt_id, stage_id) + body:
                        # returns 202 SUBMISSION_IN_PROGRESS without hitting sgl-omni a second time!
                        r_dup = client.post("/v1/audio/speech", headers=hdrs, json={"input": "chorus"})
                        self.assertEqual(r_dup.status_code, 202)
                        self.assertEqual(r_dup.json()["error_code"], "SUBMISSION_IN_PROGRESS")
                        self.assertEqual(upstream_calls["n"], 1)

                        # Different body on same attempt/stage -> 409
                        r_conflict = client.post("/v1/audio/speech", headers=hdrs, json={"input": "verse"})
                        self.assertEqual(r_conflict.status_code, 409)
                        self.assertEqual(upstream_calls["n"], 1)

                        release_upstream.set()
                        r1 = fut1.result(timeout=5.0)
                        self.assertEqual(r1.status_code, 200)
                        self.assertEqual(r1.content, b"RIFF-DEDUP-WAV")
                        self.assertEqual(upstream_calls["n"], 1)

                    # Lost-response retry replays cached 200 + X-VF-Reclaim-Proof even after proxy restart!
                    app_reloaded = create_music3_guard_app(
                        upstream_url="http://127.0.0.1:18000",
                        state_path=state_path,
                        transport=BlockingAsyncTransport(),
                    )
                    with TestClient(app_reloaded) as client_reloaded:
                        r_cached = client_reloaded.post("/v1/audio/speech", headers=hdrs, json={"input": "chorus"})
                        self.assertEqual(r_cached.status_code, 200)
                        self.assertEqual(r_cached.headers.get("x-vf-dedup-replay"), "cached_response")
                        self.assertEqual(r_cached.content, b"RIFF-DEDUP-WAV")
                        self.assertTrue(json.loads(r_cached.headers["x-vf-reclaim-proof"])["reclaimed"])
                        self.assertEqual(upstream_calls["n"], 1)

                        # Explicit /revoke-lease freezes cached replay -> 409
                        r_rev = client_reloaded.post(
                            "/internal/v1/gpu-residency/revoke-lease",
                            headers={"Authorization": "Bearer sched-music3-secret"},
                            json={"lease_id": "lease-m3-dedup-1", "fencing_token": 95},
                        )
                        self.assertEqual(r_rev.status_code, 200)
                        r_after_revoke = client_reloaded.post("/v1/audio/speech", headers=hdrs, json={"input": "chorus"})
                        self.assertEqual(r_after_revoke.status_code, 409)
                        self.assertEqual(upstream_calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
