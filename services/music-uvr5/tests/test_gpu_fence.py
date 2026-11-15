from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import verdantflare_uvr5.api as uvr5_api
from verdantflare_uvr5.gpu_guard import FencePersistenceError, LeaseFenceGuard
from verdantflare_uvr5.service import SeparationFailed, UVR5Service


class UVR5GpuFenceAndVramCleanupTest(unittest.TestCase):
    def test_separate_clears_cuda_cache_on_success_and_error(self) -> None:
        class FailingSeparator:
            def load_model(self, model_filename):
                raise SeparationFailed("boom")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / "in.wav"
            src.write_bytes(b"audio")
            svc = UVR5Service(root)
            with (
                patch.object(svc, "_separator", return_value=FailingSeparator()),
                patch("verdantflare_uvr5.service.clear_cuda_cache") as clear_mock,
            ):
                with self.assertRaises(SeparationFailed):
                    svc.separate(src, root)
                clear_mock.assert_called_once()
            self.assertEqual(svc.active_tasks, 0)

    def test_api_six_tuple_fence_durable_sealing_and_internal_evict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_root = Path(directory)
            fake_zip = tmp_root / "stems.zip"
            fake_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            state_path = tmp_root / "guard_state.json"

            ident = {
                "pod_uid": "pod-uvr5-1",
                "container_id": "containerd://cid-uvr5-1",
                "boot_id": "boot-node-1",
                "supervisor_pid": 4200,
                "supervisor_starttime_ticks": 88000,
                "visible_gpu_uuids": ["GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
            }
            guard = LeaseFenceGuard(state_path=state_path)
            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-secret-token",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(uvr5_api, "TEMP_ROOT", tmp_root),
                patch.object(uvr5_api, "fence_guard", guard),
                patch("verdantflare_uvr5.api.read_runtime_identity", return_value=ident),
                patch("verdantflare_uvr5.gpu_guard.read_runtime_identity", return_value=ident),
                patch.object(uvr5_api.service, "separate", return_value=fake_zip),
            ):
                client = TestClient(uvr5_api.app)

                # Missing grant headers -> 403 under require
                r_missing = client.post(
                    "/v1/audio/stem-separations",
                    files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                )
                self.assertEqual(r_missing.status_code, 403)

                # Missing expires_at / identity -> 403
                r_incomplete = client.post(
                    "/v1/audio/stem-separations",
                    headers={
                        "X-VF-GPU-Lease-ID": "lease-uvr5-1",
                        "X-VF-GPU-Fencing-Token": "10",
                        "X-VF-GPU-Attempt-ID": "att-1",
                        "X-VF-GPU-Stage-ID": "uvr5_separate",
                        "X-VF-GPU-Pod-UID": "pod-uvr5-1",
                        "X-VF-GPU-Container-ID": "cid-uvr5-1",
                        "X-VF-GPU-Boot-ID": "boot-node-1",
                        "X-VF-GPU-UUID": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    },
                    files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                )
                self.assertEqual(r_incomplete.status_code, 403)

                # Valid 6-tuple headers -> 200 + receiver_guard X-VF-Reclaim-Proof
                valid_headers = {
                    "X-VF-GPU-Lease-ID": "lease-uvr5-1",
                    "X-VF-GPU-Fencing-Token": "10",
                    "X-VF-GPU-Attempt-ID": "att-1",
                    "X-VF-GPU-Stage-ID": "uvr5_separate",
                    "X-VF-GPU-Parent-Task-ID": "task-uvr5-1",
                    "X-VF-GPU-Pod-UID": "pod-uvr5-1",
                    "X-VF-GPU-Container-ID": "cid-uvr5-1",
                    "X-VF-GPU-Boot-ID": "boot-node-1",
                    "X-VF-GPU-UUID": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                }
                r_ok = client.post(
                    "/v1/audio/stem-separations",
                    headers=valid_headers,
                    files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                )
                self.assertEqual(r_ok.status_code, 200)
                proof = json.loads(r_ok.headers["x-vf-reclaim-proof"])
                self.assertTrue(proof["reclaimed"])
                self.assertEqual(proof["proof_source"], "receiver_guard")
                self.assertTrue(proof["token_sealed"])
                self.assertEqual(proof["sealed_pod_uid"], "pod-uvr5-1")
                self.assertEqual(proof["consumed_fencing_token"], 10)

                # Post-completion replay with new attempt_id MUST fail 409 even across guard restart!
                guard_reloaded = LeaseFenceGuard(state_path=state_path)
                with patch.object(uvr5_api, "fence_guard", guard_reloaded):
                    replay_headers = dict(valid_headers)
                    replay_headers["X-VF-GPU-Attempt-ID"] = "att-replay"
                    r_replay = client.post(
                        "/v1/audio/stem-separations",
                        headers=replay_headers,
                        files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                    )
                    self.assertEqual(r_replay.status_code, 409)

                # /internal/v1/gpu-guard/reclaim returns receiver_guard proof
                r_rec = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-secret-token"},
                    json={"lease_id": "lease-uvr5-rec", "fencing_token": 11},
                )
                self.assertEqual(r_rec.status_code, 200)
                self.assertTrue(r_rec.json()["reclaim_proof"]["token_sealed"])

                # Internal evict-vram requires Bearer auth
                self.assertEqual(client.post("/internal/v1/gpu-residency/evict-vram").status_code, 401)
                r_evict = client.post(
                    "/internal/v1/gpu-residency/evict-vram",
                    headers={"Authorization": "Bearer sched-secret-token"},
                )
                self.assertEqual(r_evict.status_code, 200)
                self.assertEqual(r_evict.json()["residency_state"], "S3")

                # Revoke fencing token 15 -> token 12 rejected with 409
                r_rev = client.post(
                    "/internal/v1/gpu-residency/revoke-lease",
                    headers={"Authorization": "Bearer sched-secret-token"},
                    json={"lease_id": "lease-uvr5-2", "fencing_token": 15},
                )
                self.assertEqual(r_rev.status_code, 200)
                stale_headers = dict(valid_headers)
                stale_headers.update(
                    {
                        "X-VF-GPU-Lease-ID": "lease-uvr5-3",
                        "X-VF-GPU-Fencing-Token": "12",
                        "X-VF-GPU-Attempt-ID": "att-2",
                        "X-VF-GPU-Parent-Task-ID": "task-uvr5-2",
                    }
                )
                r_stale = client.post(
                    "/v1/audio/stem-separations",
                    headers=stale_headers,
                    files={"audio": ("song2.wav", io.BytesIO(b"wavbytes2"), "audio/wav")},
                )
                self.assertEqual(r_stale.status_code, 409)

    def test_concurrent_separate_blocks_reclaim_and_evict_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_root = Path(directory)
            fake_zip = tmp_root / "stems.zip"
            fake_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            state_path = tmp_root / "guard_concurrent.json"
            ident = {
                "pod_uid": "pod-uvr5-c",
                "container_id": "containerd://cid-uvr5-c",
                "boot_id": "boot-node-c",
                "supervisor_pid": os.getpid(),
                "supervisor_starttime_ticks": 88001,
                "visible_gpu_uuids": ["GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
            }
            guard = LeaseFenceGuard(state_path=state_path)
            entered = threading.Barrier(2, timeout=5.0)
            release = threading.Barrier(2, timeout=5.0)

            def blocking_separate(src, dst):
                entered.wait()
                release.wait()
                return fake_zip

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-secret-token",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(uvr5_api, "TEMP_ROOT", tmp_root),
                patch.object(uvr5_api, "fence_guard", guard),
                patch("verdantflare_uvr5.api.read_runtime_identity", return_value=ident),
                patch("verdantflare_uvr5.gpu_guard.read_runtime_identity", return_value=ident),
                patch.object(uvr5_api.service, "separate", side_effect=blocking_separate),
            ):
                client = TestClient(uvr5_api.app)
                worker_resp: dict[str, object] = {}

                def run_worker() -> None:
                    worker_resp["r"] = client.post(
                        "/v1/audio/stem-separations",
                        headers={
                            "X-VF-GPU-Lease-ID": "lease-uvr5-c1",
                            "X-VF-GPU-Fencing-Token": "50",
                            "X-VF-GPU-Attempt-ID": "att-c1",
                            "X-VF-GPU-Stage-ID": "uvr5_separate",
                            "X-VF-GPU-Parent-Task-ID": "task-uvr5-c1",
                            "X-VF-GPU-Pod-UID": "pod-uvr5-c",
                            "X-VF-GPU-Container-ID": "cid-uvr5-c",
                            "X-VF-GPU-Boot-ID": "boot-node-c",
                            "X-VF-GPU-UUID": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                        },
                        files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                    )

                t = threading.Thread(target=run_worker)
                t.start()
                entered.wait()

                # Same (parent_task_id, attempt_id, stage_id) + body while in-flight -> 202 SUBMISSION_IN_PROGRESS without 2nd separate()!
                r_dup = client.post(
                    "/v1/audio/stem-separations",
                    headers={
                        "X-VF-GPU-Lease-ID": "lease-uvr5-c1",
                        "X-VF-GPU-Fencing-Token": "50",
                        "X-VF-GPU-Attempt-ID": "att-c1",
                        "X-VF-GPU-Stage-ID": "uvr5_separate",
                        "X-VF-GPU-Parent-Task-ID": "task-uvr5-c1",
                        "X-VF-GPU-Pod-UID": "pod-uvr5-c",
                        "X-VF-GPU-Container-ID": "cid-uvr5-c",
                        "X-VF-GPU-Boot-ID": "boot-node-c",
                        "X-VF-GPU-UUID": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                        "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                    },
                    files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                )
                self.assertEqual(r_dup.status_code, 202)
                self.assertEqual(r_dup.json()["error_code"], "SUBMISSION_IN_PROGRESS")

                r_rec_busy = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-secret-token"},
                    json={"lease_id": "lease-uvr5-c1", "fencing_token": 50},
                )
                self.assertEqual(r_rec_busy.status_code, 409)
                self.assertFalse(r_rec_busy.json()["reclaim_proof"]["reclaimed"])

                r_ev_busy = client.post(
                    "/internal/v1/gpu-residency/evict-vram",
                    headers={"Authorization": "Bearer sched-secret-token"},
                )
                self.assertEqual(r_ev_busy.status_code, 409)

                release.wait()
                t.join(timeout=5.0)
                self.assertEqual(worker_resp["r"].status_code, 200)  # type: ignore[union-attr]
                self.assertEqual(uvr5_api.service.separate.call_count, 1)  # type: ignore[attr-defined]

                r_rec_idle = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-secret-token"},
                    json={"lease_id": "lease-uvr5-c2", "fencing_token": 51},
                )
                self.assertEqual(r_rec_idle.status_code, 200)
                self.assertTrue(r_rec_idle.json()["reclaim_proof"]["reclaimed"])

    def test_short_write_enospc_and_corrupt_state_fail_closed_503(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_root = Path(directory)
            state_path = tmp_root / "guard_short.json"
            ident = {
                "pod_uid": "pod-uvr5-f",
                "container_id": "cid-uvr5-f",
                "boot_id": "boot-node-f",
                "supervisor_pid": os.getpid(),
                "supervisor_starttime_ticks": 88002,
                "visible_gpu_uuids": ["GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"],
            }

            def one_byte_write(fd: int, buf: memoryview) -> int:
                return os.write(fd, buf[:1])

            with patch.dict(os.environ, {"MUSIC_GPU_GRANT_POLICY": "require"}, clear=False):
                g_short = LeaseFenceGuard(state_path=state_path, write_fn=one_byte_write)
                proof = g_short.reclaim_and_seal(lease_id="l-short", fencing_token=61, identity=ident)
                self.assertIsNotNone(proof)
                self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["min_fencing_token"], 61)

                def enospc_write(_fd: int, _buf: memoryview) -> int:
                    raise OSError(28, "No space left on device")

                g_enospc = LeaseFenceGuard(state_path=tmp_root / "enospc.json", write_fn=enospc_write)
                with self.assertRaises(FencePersistenceError):
                    g_enospc.reclaim_and_seal(lease_id="l-enospc", fencing_token=62, identity=ident)

                corrupt_path = tmp_root / "corrupt.json"
                corrupt_path.write_text("{corrupt-json", encoding="utf-8")
                g_corrupt = LeaseFenceGuard(state_path=corrupt_path)
                with (
                    patch.dict(os.environ, {"STATION_GPU_SCHEDULER_TOKEN": "sched-secret-token"}, clear=False),
                    patch.object(uvr5_api, "TEMP_ROOT", tmp_root),
                    patch.object(uvr5_api, "fence_guard", g_corrupt),
                    patch("verdantflare_uvr5.api.read_runtime_identity", return_value=ident),
                ):
                    client = TestClient(uvr5_api.app)
                    r_work = client.post(
                        "/v1/audio/stem-separations",
                        headers={
                            "X-VF-GPU-Lease-ID": "lease-corrupt",
                            "X-VF-GPU-Fencing-Token": "70",
                            "X-VF-GPU-Attempt-ID": "att-1",
                            "X-VF-GPU-Stage-ID": "uvr5_separate",
                            "X-VF-GPU-Pod-UID": "pod-uvr5-f",
                            "X-VF-GPU-Container-ID": "cid-uvr5-f",
                            "X-VF-GPU-Boot-ID": "boot-node-f",
                            "X-VF-GPU-UUID": "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                        },
                        files={"audio": ("song.wav", io.BytesIO(b"wavbytes"), "audio/wav")},
                    )
                    self.assertEqual(r_work.status_code, 503)
                    r_rec = client.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-secret-token"},
                        json={"lease_id": "lease-corrupt", "fencing_token": 70},
                    )
                    self.assertEqual(r_rec.status_code, 503)


if __name__ == "__main__":
    unittest.main()
