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
from guard.proxy import create_acestep_guard_app


class ACEStepGuardProxyTest(unittest.TestCase):
    def test_release_task_fence_query_settlement_and_restart_sealing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "acestep_guard.json"
            ident = {
                "pod_uid": "pod-acestep-1",
                "container_id": "cid-acestep-1",
                "boot_id": "boot-acestep-1",
                "supervisor_pid": 4500,
                "supervisor_starttime_ticks": 75000,
                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
            }
            poll_seq = {"n": 0}

            def upstream_handler(req: httpx.Request) -> httpx.Response:
                if req.url.path == "/health":
                    return httpx.Response(200, json={"status": "ok"})
                if req.url.path == "/release_task":
                    return httpx.Response(200, json={"code": 200, "data": {"task_id": "ace-task-1"}})
                if req.url.path == "/query_result":
                    poll_seq["n"] += 1
                    st = 0 if poll_seq["n"] <= 2 else 1
                    return httpx.Response(
                        200,
                        json={"code": 200, "data": [{"task_id": "ace-task-1", "status": st, "result": "[]"}]},
                    )
                return httpx.Response(404)

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-ace-secret",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch("guard.proxy.read_runtime_identity", return_value=ident),
                patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
            ):
                app = create_acestep_guard_app(
                    upstream_url="http://127.0.0.1:18001",
                    state_path=state_path,
                    transport=httpx.MockTransport(upstream_handler),
                )
                client = TestClient(app)

                # Missing grant rejected 403
                self.assertEqual(client.post("/release_task", data={"task_type": "repaint"}).status_code, 403)

                valid_hdrs = {
                    "X-VF-GPU-Lease-ID": "lease-ace-1",
                    "X-VF-GPU-Fencing-Token": "41",
                    "X-VF-GPU-Attempt-ID": "att-1",
                    "X-VF-GPU-Stage-ID": "ace_step_repaint",
                    "X-VF-GPU-Parent-Task-ID": "task-ace-parent-1",
                    "X-VF-GPU-Pod-UID": "pod-acestep-1",
                    "X-VF-GPU-Container-ID": "cid-acestep-1",
                    "X-VF-GPU-Boot-ID": "boot-acestep-1",
                    "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                    "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                }
                r_sub = client.post("/release_task", headers=valid_hdrs, data={"task_type": "repaint"})
                self.assertEqual(r_sub.status_code, 200)

                # While status==0, reclaim endpoint reports 409 active_tasks==1
                r_rec_busy = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-ace-secret"},
                    json={"lease_id": "lease-ace-1", "fencing_token": 41},
                )
                self.assertEqual(r_rec_busy.status_code, 409)

                # First poll: status==0 -> no reclaim_proof yet
                q1 = client.post("/query_result", json={"task_id_list": ["ace-task-1"]})
                self.assertNotIn("x-vf-reclaim-proof", q1.headers)

                # Second poll: status==1 -> seals token 41 and emits receiver_guard proof!
                q2 = client.post("/query_result", json={"task_id_list": ["ace-task-1"]})
                proof = json.loads(q2.headers["x-vf-reclaim-proof"])
                self.assertTrue(proof["reclaimed"])
                self.assertEqual(proof["proof_source"], "receiver_guard")
                self.assertTrue(proof["token_sealed"])
                self.assertEqual(proof["consumed_fencing_token"], 41)

                # Restart proxy on same state_path -> token 41 replay rejected 409!
                app2 = create_acestep_guard_app(
                    upstream_url="http://127.0.0.1:18001",
                    state_path=state_path,
                    transport=httpx.MockTransport(upstream_handler),
                )
                client2 = TestClient(app2)
                replay_hdrs = dict(valid_hdrs)
                replay_hdrs["X-VF-GPU-Attempt-ID"] = "att-2"
                self.assertEqual(client2.post("/release_task", headers=replay_hdrs, data={"task_type": "repaint"}).status_code, 409)

    def test_upstream_timeout_and_proxy_restart_reconcile_and_resident_procs_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "ace_uncertain.json"
            ident = {
                "pod_uid": "pod-acestep-2",
                "container_id": "cid-acestep-2",
                "boot_id": "boot-acestep-2",
                "supervisor_pid": 4600,
                "supervisor_starttime_ticks": 76000,
                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
            }
            # Spawn a real sleeper subprocess to simulate live upstream acestep.api_server
            upstream_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                mode = {"behavior": "timeout", "task_status": 0}

                def upstream_handler(req: httpx.Request) -> httpx.Response:
                    if req.url.path == "/health":
                        return httpx.Response(200, json={"status": "ok"})
                    if req.url.path == "/release_task":
                        if mode["behavior"] == "timeout":
                            raise httpx.ReadTimeout("upstream timed out after accepting work")
                        return httpx.Response(200, json={"code": 200, "data": {"task_id": "ace-restart-t1"}})
                    if req.url.path == "/query_result":
                        return httpx.Response(
                            200,
                            json={"code": 200, "data": [{"task_id": "ace-restart-t1", "status": mode["task_status"], "result": "[]"}]},
                        )
                    return httpx.Response(404)

                env = {
                    "MUSIC_GPU_GRANT_POLICY": "require",
                    "STATION_GPU_SCHEDULER_TOKEN": "sched-ace-secret",
                }
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch("guard.proxy.read_runtime_identity", return_value=ident),
                    patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
                ):
                    app1 = create_acestep_guard_app(
                        upstream_url="http://127.0.0.1:18001",
                        state_path=state_path,
                        upstream_leader_pid=upstream_proc.pid,
                        transport=httpx.MockTransport(upstream_handler),
                    )
                    c1 = TestClient(app1)
                    hdrs1 = {
                        "X-VF-GPU-Lease-ID": "lease-ace-u1",
                        "X-VF-GPU-Fencing-Token": "81",
                        "X-VF-GPU-Attempt-ID": "att-u1",
                        "X-VF-GPU-Stage-ID": "ace_step_repaint",
                        "X-VF-GPU-Parent-Task-ID": "task-ace-u1",
                        "X-VF-GPU-Pod-UID": "pod-acestep-2",
                        "X-VF-GPU-Container-ID": "cid-acestep-2",
                        "X-VF-GPU-Boot-ID": "boot-acestep-2",
                        "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                        "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                    }
                    # Upstream times out -> returns 504 and marks record uncertain!
                    r_to = c1.post("/release_task", headers=hdrs1, data={"task_type": "repaint"})
                    self.assertEqual(r_to.status_code, 504)

                    # Restart proxy while SAME upstream process is still alive -> /reclaim MUST refuse 409!
                    app2 = create_acestep_guard_app(
                        upstream_url="http://127.0.0.1:18001",
                        state_path=state_path,
                        upstream_leader_pid=upstream_proc.pid,
                        transport=httpx.MockTransport(upstream_handler),
                    )
                    c2 = TestClient(app2)
                    r_rec_busy = c2.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-ace-secret"},
                        json={"lease_id": "lease-ace-u1", "fencing_token": 81},
                    )
                    self.assertEqual(r_rec_busy.status_code, 409)
                    self.assertFalse(r_rec_busy.json()["reclaim_proof"]["reclaimed"])

                    # Terminate upstream process -> dead upstream leader causes /health 503 and /reclaim 409 (upstream_alive=False)
                    upstream_proc.terminate()
                    upstream_proc.wait(timeout=5.0)
                    self.assertEqual(c2.get("/health").status_code, 503)
                    r_rec_dead_up = c2.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-ace-secret"},
                        json={"lease_id": "lease-ace-u1", "fencing_token": 81},
                    )
                    self.assertEqual(r_rec_dead_up.status_code, 409)
                    self.assertFalse(r_rec_dead_up.json()["reclaim_proof"]["upstream_alive"])

                # Now spawn a fresh upstream + synthetic /proc hierarchy (init + upstream + child worker)
                upstream_proc2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
                try:
                    proc_root = root / "fake_proc"
                    # Linux /proc/<pid>/stat format: pid (comm) state ppid ... (starttime is index 19 after state)
                    def write_stat(pid: int, ppid: int, ticks: int) -> None:
                        pdir = proc_root / str(pid)
                        pdir.mkdir(parents=True, exist_ok=True)
                        fields = ["S", str(ppid)] + ["0"] * 17 + [str(ticks)] + ["0"] * 5
                        (pdir / "stat").write_text(f"{pid} (python) " + " ".join(fields), encoding="utf-8")

                    write_stat(1, 0, 100)
                    write_stat(4600, 1, 76000)
                    write_stat(upstream_proc2.pid, 1, 88100)
                    write_stat(9901, upstream_proc2.pid, 88200)

                    mode["behavior"] = "ok"
                    mode["task_status"] = 0
                    with (
                        patch.dict(os.environ, env, clear=False),
                        patch("guard.proxy.read_runtime_identity", return_value=ident),
                        patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
                    ):
                        app3 = create_acestep_guard_app(
                            upstream_url="http://127.0.0.1:18001",
                            state_path=state_path,
                            init_pid=1,
                            upstream_leader_pid=upstream_proc2.pid,
                            proc_root=proc_root,
                            transport=httpx.MockTransport(upstream_handler),
                        )
                        c3 = TestClient(app3)
                        # Fresh upstream has new (pid, ticks), so uncertain record from old upstream was pruned!
                        hdrs2 = dict(hdrs1)
                        hdrs2.update(
                            {
                                "X-VF-GPU-Lease-ID": "lease-ace-u2",
                                "X-VF-GPU-Fencing-Token": "85",
                                "X-VF-GPU-Attempt-ID": "att-u2",
                                "X-VF-GPU-Parent-Task-ID": "task-ace-u2",
                            }
                        )
                        self.assertEqual(c3.post("/release_task", headers=hdrs2, data={"task_type": "repaint"}).status_code, 200)

                        # Restart proxy while ace-restart-t1 is still running -> active /query_result reconciliation on /reclaim!
                        app4 = create_acestep_guard_app(
                            upstream_url="http://127.0.0.1:18001",
                            state_path=state_path,
                            init_pid=1,
                            upstream_leader_pid=upstream_proc2.pid,
                            proc_root=proc_root,
                            transport=httpx.MockTransport(upstream_handler),
                        )
                        c4 = TestClient(app4)
                        self.assertEqual(
                            c4.post(
                                "/internal/v1/gpu-guard/reclaim",
                                headers={"Authorization": "Bearer sched-ace-secret"},
                                json={"lease_id": "lease-ace-u2", "fencing_token": 85},
                            ).status_code,
                            409,
                        )
                        # Upstream finishes task -> /reclaim actively queries /query_result, settles, and attests resident_procs!
                        mode["task_status"] = 1
                        r_settled = c4.post(
                            "/internal/v1/gpu-guard/reclaim",
                            headers={"Authorization": "Bearer sched-ace-secret"},
                            json={"lease_id": "lease-ace-u2", "fencing_token": 85},
                        )
                        self.assertEqual(r_settled.status_code, 200)
                        proof = r_settled.json()["reclaim_proof"]
                        self.assertTrue(proof["reclaimed"])
                        self.assertEqual(
                            proof["resident_procs"],
                            [
                                {"pid": 1, "starttime_ticks": 100},
                                {"pid": upstream_proc2.pid, "starttime_ticks": 88100},
                                {"pid": 9901, "starttime_ticks": 88200},
                            ]
                            if upstream_proc2.pid < 9901
                            else sorted(
                                [
                                    {"pid": 1, "starttime_ticks": 100},
                                    {"pid": upstream_proc2.pid, "starttime_ticks": 88100},
                                    {"pid": 9901, "starttime_ticks": 88200},
                                ],
                                key=lambda x: x["pid"],
                            ),
                        )

                        # Corrupt state file fails closed 503
                        corrupt_path = root / "ace_corrupt.json"
                        corrupt_path.write_text("{corrupt", encoding="utf-8")
                        app_bad = create_acestep_guard_app(
                            upstream_url="http://127.0.0.1:18001",
                            state_path=corrupt_path,
                            transport=httpx.MockTransport(upstream_handler),
                        )
                        c_bad = TestClient(app_bad)
                        self.assertEqual(c_bad.post("/release_task", headers=hdrs2, data={"task_type": "repaint"}).status_code, 503)
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

    def test_same_attempt_stage_inflight_and_lost_response_replay_never_double_submits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "ace_dedup.json"
            ident = {
                "pod_uid": "pod-acestep-dedup",
                "container_id": "cid-acestep-dedup",
                "boot_id": "boot-acestep-dedup",
                "supervisor_pid": 4700,
                "supervisor_starttime_ticks": 77000,
                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
            }
            entered_upstream = threading.Event()
            release_upstream = threading.Event()
            upstream_submit_calls = {"n": 0}

            class BlockingAsyncTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, req: httpx.Request) -> httpx.Response:
                    if req.url.path == "/release_task":
                        upstream_submit_calls["n"] += 1
                        entered_upstream.set()
                        await __import__("anyio").to_thread.run_sync(release_upstream.wait, 5.0)
                        return httpx.Response(200, json={"code": 200, "data": {"task_id": "ace-dedup-t1"}})
                    if req.url.path == "/query_result":
                        return httpx.Response(
                            200,
                            json={"code": 200, "data": [{"task_id": "ace-dedup-t1", "status": 0, "result": "[]"}]},
                        )
                    return httpx.Response(200, json={"status": "ok"})

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-ace-secret",
            }
            hdrs = {
                "X-VF-GPU-Lease-ID": "lease-ace-dedup-1",
                "X-VF-GPU-Fencing-Token": "91",
                "X-VF-GPU-Attempt-ID": "att-dedup-1",
                "X-VF-GPU-Stage-ID": "ace_step_repaint",
                "X-VF-GPU-Parent-Task-ID": "task-ace-dedup-1",
                "X-VF-GPU-Pod-UID": "pod-acestep-dedup",
                "X-VF-GPU-Container-ID": "cid-acestep-dedup",
                "X-VF-GPU-Boot-ID": "boot-acestep-dedup",
                "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch("guard.proxy.read_runtime_identity", return_value=ident),
                patch("guard.gpu_guard.read_runtime_identity", return_value=ident),
            ):
                app = create_acestep_guard_app(
                    upstream_url="http://127.0.0.1:18001",
                    state_path=state_path,
                    transport=BlockingAsyncTransport(),
                )
                with TestClient(app) as client:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        fut1 = pool.submit(client.post, "/release_task", headers=hdrs, data={"task_type": "repaint"})
                        self.assertTrue(entered_upstream.wait(timeout=5.0))

                        # Second concurrent request with identical (parent_task_id, attempt_id, stage_id) + body:
                        # MUST NOT forward to upstream a second time; returns 202 SUBMISSION_IN_PROGRESS!
                        r_dup_inflight = client.post("/release_task", headers=hdrs, data={"task_type": "repaint"})
                        self.assertEqual(r_dup_inflight.status_code, 202)
                        self.assertEqual(r_dup_inflight.json()["error_code"], "SUBMISSION_IN_PROGRESS")
                        self.assertEqual(upstream_submit_calls["n"], 1)

                        # Conflicting body on same attempt/stage still fails 409
                        r_conflict = client.post("/release_task", headers=hdrs, data={"task_type": "different"})
                        self.assertEqual(r_conflict.status_code, 409)
                        self.assertEqual(upstream_submit_calls["n"], 1)

                        release_upstream.set()
                        r1 = fut1.result(timeout=5.0)
                        self.assertEqual(r1.status_code, 200)
                        self.assertEqual(r1.json()["data"]["task_id"], "ace-dedup-t1")
                        self.assertEqual(upstream_submit_calls["n"], 1)

                    # Lost-response retry after first completed replays cached 200 without calling upstream again!
                    app_reloaded = create_acestep_guard_app(
                        upstream_url="http://127.0.0.1:18001",
                        state_path=state_path,
                        transport=BlockingAsyncTransport(),
                    )
                    with TestClient(app_reloaded) as client_reloaded:
                        r_cached = client_reloaded.post("/release_task", headers=hdrs, data={"task_type": "repaint"})
                        self.assertEqual(r_cached.status_code, 200)
                        self.assertEqual(r_cached.headers.get("x-vf-dedup-replay"), "cached_response")
                        self.assertEqual(r_cached.json()["data"]["task_id"], "ace-dedup-t1")
                        self.assertEqual(upstream_submit_calls["n"], 1)

                        # Explicit /revoke-lease freezes cached replay -> 409
                        r_rev = client_reloaded.post(
                            "/internal/v1/gpu-residency/revoke-lease",
                            headers={"Authorization": "Bearer sched-ace-secret"},
                            json={"lease_id": "lease-ace-dedup-1", "fencing_token": 91},
                        )
                        self.assertEqual(r_rev.status_code, 200)
                        r_after_revoke = client_reloaded.post("/release_task", headers=hdrs, data={"task_type": "repaint"})
                        self.assertEqual(r_after_revoke.status_code, 409)
                        self.assertEqual(upstream_submit_calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
