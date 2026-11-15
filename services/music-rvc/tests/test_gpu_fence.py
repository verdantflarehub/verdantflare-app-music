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
import numpy as np

import verdantflare_rvc.api as rvc_api
from verdantflare_rvc.catalog import VoiceModelCatalog
from verdantflare_rvc.gpu_guard import FencePersistenceError, LeaseFenceGuard
from verdantflare_rvc.service import RVCService


class RVCGpuFenceTest(unittest.TestCase):
    def test_rvc_six_tuple_fence_durable_sealing_and_idle_model_evict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voices = root / "voices"
            (voices / "lead-vocal").mkdir(parents=True)
            (voices / "lead-vocal" / "model.pth").write_bytes(b"ckpt")
            catalog = VoiceModelCatalog(voices)
            svc = RVCService(catalog)
            svc._vc = object()
            svc._loaded_model_id = "lead-vocal"
            state_path = root / "rvc_guard_state.json"

            ident = {
                "pod_uid": "pod-rvc-1",
                "container_id": "docker://cid-rvc-1",
                "boot_id": "boot-node-1",
                "supervisor_pid": 5100,
                "supervisor_starttime_ticks": 91000,
                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
            }
            guard = LeaseFenceGuard(state_path=state_path)
            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-rvc-token",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(rvc_api, "TEMP_ROOT", root / "tmp"),
                patch.object(rvc_api, "catalog", catalog),
                patch.object(rvc_api, "service", svc),
                patch.object(rvc_api, "fence_guard", guard),
                patch("verdantflare_rvc.api.read_runtime_identity", return_value=ident),
                patch("verdantflare_rvc.gpu_guard.read_runtime_identity", return_value=ident),
                patch.object(
                    svc,
                    "convert",
                    return_value=(40000, np.zeros(400, dtype=np.float32)),
                ),
            ):
                client = TestClient(rvc_api.app)

                # Missing grant -> 403
                r_missing = client.post(
                    "/v1/audio/voice-conversions",
                    data={"model_id": "lead-vocal"},
                    files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                )
                self.assertEqual(r_missing.status_code, 403)

                # Substring prefix container_id mismatch ("cid-rvc" != "cid-rvc-1") MUST fail 403!
                bad_cid_headers = {
                    "X-VF-GPU-Lease-ID": "lease-rvc-1",
                    "X-VF-GPU-Fencing-Token": "21",
                    "X-VF-GPU-Attempt-ID": "att-rvc-1",
                    "X-VF-GPU-Stage-ID": "rvc_convert",
                    "X-VF-GPU-Parent-Task-ID": "task-rvc-1",
                    "X-VF-GPU-Pod-UID": "pod-rvc-1",
                    "X-VF-GPU-Container-ID": "cid-rvc",
                    "X-VF-GPU-Boot-ID": "boot-node-1",
                    "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                    "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                }
                r_bad_cid = client.post(
                    "/v1/audio/voice-conversions",
                    headers=bad_cid_headers,
                    data={"model_id": "lead-vocal"},
                    files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                )
                self.assertEqual(r_bad_cid.status_code, 403)

                # Valid 6-tuple -> 200 + receiver_guard X-VF-Reclaim-Proof
                headers = dict(bad_cid_headers)
                headers["X-VF-GPU-Container-ID"] = "cid-rvc-1"
                r_ok = client.post(
                    "/v1/audio/voice-conversions",
                    headers=headers,
                    data={"model_id": "lead-vocal"},
                    files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                )
                self.assertEqual(r_ok.status_code, 200)
                proof = json.loads(r_ok.headers["x-vf-reclaim-proof"])
                self.assertTrue(proof["reclaimed"])
                self.assertEqual(proof["proof_source"], "receiver_guard")
                self.assertTrue(proof["token_sealed"])
                self.assertEqual(proof["consumed_fencing_token"], 21)

                # Reload persisted state -> token 21 replay rejected 409!
                guard2 = LeaseFenceGuard(state_path=state_path)
                with patch.object(rvc_api, "fence_guard", guard2):
                    replay_hdrs = dict(headers)
                    replay_hdrs["X-VF-GPU-Attempt-ID"] = "att-rvc-2"
                    r_replay = client.post(
                        "/v1/audio/voice-conversions",
                        headers=replay_hdrs,
                        data={"model_id": "lead-vocal"},
                        files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                    )
                    self.assertEqual(r_replay.status_code, 409)

                # /internal/v1/gpu-guard/reclaim works
                r_rec = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-rvc-token"},
                    json={"lease_id": "lease-rvc-2", "fencing_token": 22},
                )
                self.assertEqual(r_rec.status_code, 200)
                self.assertEqual(r_rec.json()["reclaim_proof"]["consumed_fencing_token"], 22)

                # Idle evict unloads cached RVC model (S2 -> S3)
                self.assertTrue(svc.is_loaded)
                r_evict = client.post(
                    "/internal/v1/gpu-residency/evict-vram",
                    headers={"Authorization": "Bearer sched-rvc-token"},
                )
                self.assertEqual(r_evict.status_code, 200)
                self.assertFalse(svc.is_loaded)
                self.assertEqual(r_evict.json()["residency_state"], "S3")

    def test_concurrent_convert_blocks_reclaim_evict_and_corrupt_state_fails_503(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voices = root / "voices"
            (voices / "lead-vocal").mkdir(parents=True)
            (voices / "lead-vocal" / "model.pth").write_bytes(b"ckpt")
            catalog = VoiceModelCatalog(voices)
            svc = RVCService(catalog)
            svc._vc = object()
            svc._loaded_model_id = "lead-vocal"
            state_path = root / "rvc_concurrent.json"
            ident = {
                "pod_uid": "pod-rvc-c",
                "container_id": "docker://cid-rvc-c",
                "boot_id": "boot-node-c",
                "supervisor_pid": os.getpid(),
                "supervisor_starttime_ticks": 91001,
                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
            }

            def one_byte_write(fd: int, buf: memoryview) -> int:
                return os.write(fd, buf[:1])

            guard = LeaseFenceGuard(state_path=state_path, write_fn=one_byte_write)
            entered = threading.Barrier(2, timeout=5.0)
            release = threading.Barrier(2, timeout=5.0)

            def blocking_convert(**_kwargs):
                entered.wait()
                release.wait()
                return (40000, np.zeros(400, dtype=np.float32))

            env = {
                "MUSIC_GPU_GRANT_POLICY": "require",
                "STATION_GPU_SCHEDULER_TOKEN": "sched-rvc-token",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(rvc_api, "TEMP_ROOT", root / "tmp"),
                patch.object(rvc_api, "catalog", catalog),
                patch.object(rvc_api, "service", svc),
                patch.object(rvc_api, "fence_guard", guard),
                patch("verdantflare_rvc.api.read_runtime_identity", return_value=ident),
                patch("verdantflare_rvc.gpu_guard.read_runtime_identity", return_value=ident),
                patch.object(svc, "convert", side_effect=blocking_convert),
            ):
                client = TestClient(rvc_api.app)
                worker_resp: dict[str, object] = {}

                def run_worker() -> None:
                    worker_resp["r"] = client.post(
                        "/v1/audio/voice-conversions",
                        headers={
                            "X-VF-GPU-Lease-ID": "lease-rvc-c1",
                            "X-VF-GPU-Fencing-Token": "51",
                            "X-VF-GPU-Attempt-ID": "att-rvc-c1",
                            "X-VF-GPU-Stage-ID": "rvc_convert",
                            "X-VF-GPU-Parent-Task-ID": "task-rvc-c1",
                            "X-VF-GPU-Pod-UID": "pod-rvc-c",
                            "X-VF-GPU-Container-ID": "cid-rvc-c",
                            "X-VF-GPU-Boot-ID": "boot-node-c",
                            "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                            "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                        },
                        data={"model_id": "lead-vocal"},
                        files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                    )

                t = threading.Thread(target=run_worker)
                t.start()
                entered.wait()

                # Same (parent_task_id, attempt_id, stage_id) + body while in-flight -> 202 SUBMISSION_IN_PROGRESS without 2nd convert()!
                r_dup = client.post(
                    "/v1/audio/voice-conversions",
                    headers={
                        "X-VF-GPU-Lease-ID": "lease-rvc-c1",
                        "X-VF-GPU-Fencing-Token": "51",
                        "X-VF-GPU-Attempt-ID": "att-rvc-c1",
                        "X-VF-GPU-Stage-ID": "rvc_convert",
                        "X-VF-GPU-Parent-Task-ID": "task-rvc-c1",
                        "X-VF-GPU-Pod-UID": "pod-rvc-c",
                        "X-VF-GPU-Container-ID": "cid-rvc-c",
                        "X-VF-GPU-Boot-ID": "boot-node-c",
                        "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                        "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                    },
                    data={"model_id": "lead-vocal"},
                    files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                )
                self.assertEqual(r_dup.status_code, 202)
                self.assertEqual(r_dup.json()["error_code"], "SUBMISSION_IN_PROGRESS")

                r_rec_busy = client.post(
                    "/internal/v1/gpu-guard/reclaim",
                    headers={"Authorization": "Bearer sched-rvc-token"},
                    json={"lease_id": "lease-rvc-c1", "fencing_token": 51},
                )
                self.assertEqual(r_rec_busy.status_code, 409)
                self.assertFalse(r_rec_busy.json()["reclaim_proof"]["reclaimed"])

                r_ev_busy = client.post(
                    "/internal/v1/gpu-residency/evict-vram",
                    headers={"Authorization": "Bearer sched-rvc-token"},
                )
                self.assertEqual(r_ev_busy.status_code, 409)

                release.wait()
                t.join(timeout=5.0)
                self.assertEqual(worker_resp["r"].status_code, 200)  # type: ignore[union-attr]
                self.assertEqual(svc.convert.call_count, 1)  # type: ignore[attr-defined]

                # ENOSPC raises FencePersistenceError
                g_enospc = LeaseFenceGuard(
                    state_path=root / "enospc.json",
                    write_fn=lambda _fd, _b: (_ for _ in ()).throw(OSError(28, "ENOSPC")),
                )
                with self.assertRaises(FencePersistenceError):
                    g_enospc.reclaim_and_seal(lease_id="l-enospc", fencing_token=90, identity=ident)

                # Corrupt state file fails closed with 503
                corrupt_path = root / "corrupt.json"
                corrupt_path.write_text("{truncated", encoding="utf-8")
                g_corrupt = LeaseFenceGuard(state_path=corrupt_path)
                with patch.object(rvc_api, "fence_guard", g_corrupt):
                    r_c = client.post(
                        "/v1/audio/voice-conversions",
                        headers={
                            "X-VF-GPU-Lease-ID": "lease-rvc-c2",
                            "X-VF-GPU-Fencing-Token": "52",
                            "X-VF-GPU-Attempt-ID": "att-rvc-c2",
                            "X-VF-GPU-Stage-ID": "rvc_convert",
                            "X-VF-GPU-Pod-UID": "pod-rvc-c",
                            "X-VF-GPU-Container-ID": "cid-rvc-c",
                            "X-VF-GPU-Boot-ID": "boot-node-c",
                            "X-VF-GPU-UUID": "GPU-11111111-2222-3333-4444-555555555555",
                            "X-VF-GPU-Expires-At": "2099-01-01T00:00:00Z",
                        },
                        data={"model_id": "lead-vocal"},
                        files={"audio": ("vocal.wav", io.BytesIO(b"wav"), "audio/wav")},
                    )
                    self.assertEqual(r_c.status_code, 503)
                    r_rec_c = client.post(
                        "/internal/v1/gpu-guard/reclaim",
                        headers={"Authorization": "Bearer sched-rvc-token"},
                        json={"lease_id": "lease-rvc-c2", "fencing_token": 52},
                    )
                    self.assertEqual(r_rec_c.status_code, 503)


if __name__ == "__main__":
    unittest.main()
