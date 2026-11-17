from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import wave
import zipfile
from pathlib import Path

import httpx

from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs
from verdantflare_music_mcp.gpu_lease import GpuCoordinator, MemoryApi, SchedulerError, SchedulerLeaseClient


def _make_wav(duration_sec: float = 2.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = int(duration_sec * rate)
        w.writeframes(b"\x10\x00" * frames)
    return buf.getvalue()


def _make_uvr5_zip() -> bytes:
    buf = io.BytesIO()
    wav_data = _make_wav(0.5)
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("instrumental.wav", wav_data)
        zf.writestr("vocal_dry_original.wav", wav_data)
        zf.writestr("manifest.json", "{}")
    return buf.getvalue()


class MusicSchedulerP1bTest(unittest.TestCase):
    def test_release_tracks_two_heartbeats_and_retries_only_same_fence(self):
        for conflict, changed_fence in ((False, False), (True, False), (True, True)):
            with self.subTest(conflict=conflict, changed_fence=changed_fence), tempfile.TemporaryDirectory() as tmp:
                lease = {"lease_id":"lease", "fencing_token":7, "pod_uid":"pod", "gpu_uuids":["gpu"], "version":2}
                scheduler = Mock(enabled=True, ttl_seconds=90)
                scheduler.acquire_or_wait.return_value = lease
                scheduler.build_grant_headers.return_value = {}
                scheduler.heartbeat.side_effect = [{**lease,"version":3},{**lease,"version":4}]
                scheduler.get_lease.return_value = {**lease,"version":5,"fencing_token":8 if changed_fence else 7}
                versions = []
                def release(*args, **kwargs):
                    version = kwargs["expected_version"]
                    versions.append(version)
                    if version < 4 or (conflict and len(versions) == 1):
                        raise SchedulerError("LEASE_VERSION_CONFLICT", status_code=409)
                    return {"lease_id":"lease","state":"released"}
                scheduler.release.side_effect = release
                class Ticks:
                    def __init__(self): self.count = 0
                    def wait(self, interval):
                        self.count += 1
                        return self.count > 2
                    def is_set(self): return False
                    def set(self): pass
                class InlineThread:
                    def __init__(self, *, target, **kwargs): self.target = target
                    def start(self): self.target()
                    def join(self, **kwargs): pass
                gpu = GpuCoordinator(MemoryApi(), "", "", enabled=False)
                gpu.api.scales["music-minimax-music3"] = 1
                gpu.stop_deployment_and_wait = Mock()
                with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,json={
                    "runtime_identity":{"pod_uid":"pod"},
                    "reclaim_proof":{"reclaimed":True,"active_tasks":0,"proof_source":"receiver_guard","token_sealed":True},
                }))) as client:
                    executor = MusicExecutor(ArtifactStore(Path(tmp)),ServiceURLs("m","u","r","l","a"),client=client,gpu=gpu,scheduler=scheduler)
                    with patch("verdantflare_music_mcp.executor.threading.Event", Ticks), patch("verdantflare_music_mcp.executor.threading.Thread", InlineThread):
                        with executor._hold_consumer_gpu("music3", "http://worker"):
                            pass
                self.assertEqual(scheduler.heartbeat.call_count, 2)
                self.assertEqual(versions, [4,5] if conflict and not changed_fence else [4])
                self.assertEqual(scheduler.get_lease.call_count, int(conflict))

    def test_legacy_409_sealed_idle_receiver_can_stop_with_55_processes(self):
        base = {"reclaimed":False,"active_tasks":0,"proof_source":"receiver_guard",
                "token_sealed":True,"upstream_alive":True,"resident_procs":[{"pid":i+1} for i in range(55)]}
        for status, changes, expected in ((409,{},True), (200,{},True),
                (409,{"active_tasks":1},False), (409,{"token_sealed":False},False),
                (409,{"upstream_alive":False},False), (409,{"resident_procs":[{}]*129},False),
                (409,{"resident_procs":None},False), (409,{"active_tasks":"invalid"},False),
                (503,{},False)):
            with self.subTest(status=status,changes=changes), tempfile.TemporaryDirectory() as tmp:
                lease={"lease_id":"legacy", "fencing_token":1,"version":2,"pod_uid":"pod","gpu_uuids":["gpu"]}
                scheduler=Mock(enabled=True,ttl_seconds=90)
                scheduler.acquire_or_wait.return_value=lease
                scheduler.build_grant_headers.return_value={}
                def release(*args,**kwargs):
                    if not kwargs["worker_settled"]:
                        raise SchedulerError("WORKER_NOT_SETTLED",status_code=409)
                    self.assertEqual(kwargs["reclaim_mode"], "")
                    self.assertIsNone(kwargs["reclaim_proof"])
                    return {"lease_id":"legacy","state":"released"}
                scheduler.release.side_effect=release
                gpu=GpuCoordinator(MemoryApi(),"","",enabled=False)
                gpu.api.scales["music-minimax-music3"]=1
                gpu.stop_deployment_and_wait=Mock()
                def handler(req):
                    if req.url.path.endswith('/reclaim'):
                        return httpx.Response(status,json={"reclaim_proof":{**base,**changes}})
                    return httpx.Response(200,json={"runtime_identity":{"pod_uid":"pod"}})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    executor=MusicExecutor(ArtifactStore(Path(tmp)),ServiceURLs("m","u","r","l","a"),client=client,gpu=gpu,scheduler=scheduler)
                    with executor._hold_consumer_gpu("music3","http://worker"):
                        pass
                self.assertEqual(gpu.stop_deployment_and_wait.call_count, int(expected))
                self.assertEqual(scheduler.release.call_args.kwargs["worker_settled"],expected)

    def test_all_consumers_shutdown_before_release_and_never_settle_on_timeout(self):
        for consumer in ("music3", "uvr5", "rvc", "ace-step"):
            for stop_error in (None, RuntimeError("still terminating")):
                with self.subTest(consumer=consumer, stop_error=stop_error), tempfile.TemporaryDirectory() as tmp:
                    events = []
                    lease = {"lease_id":"lease", "fencing_token":1, "pod_uid":"pod", "gpu_uuids":["gpu"], "version":1}
                    scheduler = Mock(enabled=True, ttl_seconds=90)
                    scheduler.acquire_or_wait.return_value = lease
                    scheduler.build_grant_headers.return_value = {}
                    scheduler.release.side_effect = lambda *a,**kw: events.append(("release",kw["worker_settled"]))
                    gpu = GpuCoordinator(MemoryApi(),"", "", enabled=False)
                    gpu.api.scales.update({"music-minimax-music3":1,"music-uvr5":1,"music-rvc":1,"music-ace-step":1})
                    def stop(name):
                        events.append(("stop",name))
                        if stop_error:
                            raise stop_error
                    gpu.stop_deployment_and_wait = stop
                    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200,json={
                        "runtime_identity":{"pod_uid":"pod"},
                        "reclaim_proof":{"reclaimed":True,"active_tasks":0,"proof_source":"receiver_guard","token_sealed":True},
                    })))
                    with client:
                        executor = MusicExecutor(ArtifactStore(Path(tmp)),ServiceURLs("m","u","r","l","a"),client=client,gpu=gpu,scheduler=scheduler)
                        with executor._hold_consumer_gpu(consumer,"http://worker"):
                            events.append(("work",consumer))
                    self.assertEqual([e[0] for e in events],["work","stop","release"])
                    self.assertEqual(events[-1][1],stop_error is None)

    def test_music3_two_card_acquire_bind_target_and_dual_track_release_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            api = MemoryApi()
            events: list[str] = []

            class TrackedCoordinator(GpuCoordinator):
                def release(self) -> None:
                    events.append("legacy_release")
                    super().release()

            gpu = TrackedCoordinator(
                api=api,
                role="music3",
                holder="music3/test",
                deployment="music-minimax-music3",
                enabled=True,
                now=lambda: datetime(2026, 11, 15, tzinfo=timezone.utc),
            )

            sched_bodies: list[tuple[str, dict]] = []

            def sched_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                sched_bodies.append((request.url.path, body))
                if request.url.path == "/internal/v1/gpu-scheduler/leases/acquire":
                    self.assertEqual(api.get_scale("music-minimax-music3"), 0)
                    self.assertIsNone(api.get_lease("gpu-task-music3"))
                    events.append("sched_acquire")
                    return httpx.Response(
                        200,
                        json={
                            "lease_id": "lease-music3-1",
                            "state": "leased",
                            "version": 1,
                            "fencing_token": 31,
                            "attempt_id": body["attempt_id"],
                            "stage_id": body["stage_id"],
                            "gpu_uuids": [
                                "GPU-00000000-0000-0000-0000-000000000001",
                                "GPU-00000000-0000-0000-0000-000000000002",
                            ],
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    )
                if request.url.path.endswith("/bind-target"):
                    events.append("sched_bind_target")
                    return httpx.Response(
                        200,
                        json={
                            "lease_id": "lease-music3-1",
                            "state": "running",
                            "version": 2,
                            "fencing_token": 31,
                            "attempt_id": "att-1",
                            "stage_id": "music3_generate",
                            "pod_uid": body["pod_uid"],
                            "container_id": body["container_id"],
                            "boot_id": body["boot_id"],
                            "gpu_uuids": body.get("actual_gpu_uuids")
                            or [
                                "GPU-00000000-0000-0000-0000-000000000001",
                                "GPU-00000000-0000-0000-0000-000000000002",
                            ],
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    )
                if request.url.path.endswith("/release"):
                    events.append("sched_release")
                    return httpx.Response(200, json={"lease_id": "lease-music3-1", "state": "released"})
                return httpx.Response(200, json={"lease_id": "lease-music3-1", "state": "running", "fencing_token": 31})

            downstream_headers: list[dict[str, str]] = []

            def service_handler(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/health":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "runtime_identity": {
                                "pod_uid": "pod-music3-1",
                                "container_id": "cid-music3-1",
                                "boot_id": "boot-node-1",
                                "supervisor_pid": 3001,
                                "supervisor_starttime_ticks": 50001,
                                "visible_gpu_uuids": [
                                    "GPU-00000000-0000-0000-0000-000000000001",
                                    "GPU-00000000-0000-0000-0000-000000000002",
                                ],
                            },
                        },
                    )
                if request.url.path == "/internal/v1/gpu-guard/reclaim":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "settled": True,
                            "active_tasks": 0,
                            "reclaim_proof": {
                                "reclaimed": True,
                                "active_tasks": 0,
                                "proof_source": "receiver_guard",
                                "token_sealed": True,
                                "supervisor_pid": 3001,
                                "supervisor_starttime_ticks": 50001,
                                "sealed_pod_uid": "pod-music3-1",
                                "sealed_container_id": "cid-music3-1",
                                "sealed_boot_id": "boot-node-1",
                                "revoked_lease_id": "lease-music3-1",
                                "consumed_fencing_token": 31,
                            },
                        },
                    )
                downstream_headers.append(dict(request.headers))
                return httpx.Response(200, content=_make_wav(1.0))

            sched = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            executor = MusicExecutor(
                store=store,
                service_urls=ServiceURLs("http://music3", "http://uvr5", "http://rvc", "http://align", "http://mix"),
                client=httpx.Client(transport=httpx.MockTransport(service_handler)),
                gpu=gpu,
                scheduler=sched,
            )
            records = executor.generate(
                project_id="proj-1",
                lyrics="[Verse]\n你好世界",
                instructions="pop ballad",
                candidate_number=1,
                seed=7,
                max_duration_seconds=10.0,
            )
            self.assertEqual(len(records), 1)
            acq = next(b for p, b in sched_bodies if p.endswith("/acquire"))
            self.assertEqual(acq["consumer_kind"], "music3")
            self.assertEqual(acq["card_count"], 2)
            self.assertEqual(acq["peak_budget_mib"], 29696)
            self.assertEqual(acq["k8s_slots_required"], 2)
            self.assertEqual(downstream_headers[0]["x-vf-gpu-lease-id"], "lease-music3-1")
            self.assertEqual(downstream_headers[0]["x-vf-gpu-pod-uid"], "pod-music3-1")
            self.assertEqual(
                downstream_headers[0]["x-vf-gpu-uuid"],
                "GPU-00000000-0000-0000-0000-000000000001,GPU-00000000-0000-0000-0000-000000000002",
            )
            self.assertEqual(events, ["sched_acquire", "sched_bind_target", "legacy_release", "sched_release"])
            rel = next(b for p, b in sched_bodies if p.endswith("/release"))
            self.assertTrue(rel["worker_settled"])
            self.assertNotIn("reclaim_mode", rel)
            self.assertNotIn("reclaim_proof", rel)
            self.assertEqual(api.get_scale("music-minimax-music3"), 0)

    def test_uvr5_rvc_and_ace_step_acquire_slices_and_never_fabricate_missing_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            seed_art = store.create(
                project_id="proj-1",
                operation="music.generate",
                filename="source.wav",
                media_type="audio/wav",
                payload=_make_wav(4.0),
            )
            sched_calls: list[tuple[str, dict]] = []

            def sched_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                sched_calls.append((request.url.path, body))
                if request.url.path.endswith("/acquire"):
                    ck = body["consumer_kind"]
                    return httpx.Response(
                        200,
                        json={
                            "lease_id": f"lease-{ck}",
                            "state": "leased",
                            "version": 1,
                            "fencing_token": 44,
                            "attempt_id": body["attempt_id"],
                            "stage_id": body["stage_id"],
                            "pod_uid": body.get("pod_uid") or f"pod-{ck}",
                            "container_id": body.get("container_id") or f"cid-{ck}",
                            "boot_id": body.get("boot_id") or "boot-1",
                            "gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    )
                if request.url.path.endswith("/bind-target"):
                    return httpx.Response(
                        200,
                        json={
                            "lease_id": request.url.path.split("/")[-2],
                            "state": "running",
                            "version": 2,
                            "fencing_token": 44,
                            "pod_uid": body["pod_uid"],
                            "container_id": body["container_id"],
                            "boot_id": body["boot_id"],
                            "gpu_uuids": body.get("actual_gpu_uuids") or ["GPU-11111111-2222-3333-4444-555555555555"],
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    )
                lid = request.url.path.split("/")[-2]
                return httpx.Response(200, json={"lease_id": lid, "state": "released"})

            def svc_handler(request: httpx.Request) -> httpx.Response:
                host = request.url.host
                path = request.url.path
                if host in {"uvr5", "acestep"} and path == "/internal/v1/gpu-guard/reclaim":
                    return httpx.Response(200, json={"reclaim_proof": {
                        "reclaimed": True, "active_tasks": 0,
                        "proof_source": "receiver_guard", "token_sealed": True,
                    }})
                if path == "/health":
                    return httpx.Response(
                        200,
                        json={
                            "status": "ok",
                            "runtime_identity": {
                                "pod_uid": f"pod-{host}",
                                "container_id": f"cid-{host}",
                                "boot_id": "boot-1",
                                "supervisor_pid": 777,
                                "supervisor_starttime_ticks": 888,
                                "visible_gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
                            },
                        },
                    )
                if host == "uvr5" and path == "/v1/audio/stem-separations":
                    self.assertEqual(request.headers.get("x-vf-gpu-lease-id"), "lease-uvr5")
                    proof = {
                        "reclaimed": True,
                        "active_tasks": 0,
                        "proof_source": "receiver_guard",
                        "token_sealed": True,
                        "supervisor_pid": 777,
                        "supervisor_starttime_ticks": 888,
                        "sealed_pod_uid": "pod-uvr5",
                        "sealed_container_id": "cid-uvr5",
                        "sealed_boot_id": "boot-1",
                        "revoked_lease_id": "lease-uvr5",
                        "consumed_fencing_token": 44,
                    }
                    return httpx.Response(
                        200,
                        content=_make_uvr5_zip(),
                        headers={"X-VF-Reclaim-Proof": json.dumps(proof)},
                    )
                if host == "rvc" and path == "/v1/audio/voice-conversions":
                    # Simulate missing X-VF-Reclaim-Proof AND unreachable /internal/v1/gpu-guard/reclaim -> MUST NOT fake settled!
                    self.assertEqual(request.headers.get("x-vf-gpu-lease-id"), "lease-rvc")
                    return httpx.Response(200, content=_make_wav(4.0))
                if host == "rvc" and path == "/internal/v1/gpu-guard/reclaim":
                    return httpx.Response(503, json={"detail": "busy"})
                if host == "acestep" and path == "/release_task":
                    self.assertEqual(request.headers.get("x-vf-gpu-lease-id"), "lease-ace-step")
                    return httpx.Response(200, json={"code": 200, "data": {"task_id": "ace-t1"}})
                if host == "acestep" and path == "/query_result":
                    return httpx.Response(
                        200,
                        json={
                            "code": 200,
                            "data": [{"task_id": "ace-t1", "status": 1, "result": json.dumps([{"file": "/out.wav"}])}],
                            "reclaim_proof": {
                                "reclaimed": True,
                                "active_tasks": 0,
                                "proof_source": "receiver_guard",
                                "token_sealed": True,
                                "supervisor_pid": 777,
                                "supervisor_starttime_ticks": 888,
                                "sealed_pod_uid": "pod-acestep",
                                "sealed_container_id": "cid-acestep",
                                "sealed_boot_id": "boot-1",
                                "revoked_lease_id": "lease-ace-step",
                                "consumed_fencing_token": 44,
                            },
                        },
                    )
                if host == "acestep" and path == "/out.wav":
                    return httpx.Response(200, content=_make_wav(4.0))
                raise AssertionError(f"unexpected {host}{path}")

            sched = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            executor = MusicExecutor(
                store=store,
                service_urls=ServiceURLs(
                    "http://music3",
                    "http://uvr5",
                    "http://rvc",
                    "http://align",
                    "http://mix",
                    music_editor="http://acestep",
                ),
                client=httpx.Client(transport=httpx.MockTransport(svc_handler)),
                gpu=GpuCoordinator(api=MemoryApi(), role="", holder="", enabled=False),
                scheduler=sched,
            )

            executor.separate_stems(project_id="proj-1", audio_asset_id=seed_art.artifact_id)
            executor.convert_voice(
                project_id="proj-1",
                audio_asset_id=seed_art.artifact_id,
                model_id="mengsk-v2",
                pitch_shift=0,
            )
            with patch("verdantflare_music_mcp.executor.check_conditioning", return_value={"valid": True}):
                executor.redraw(
                    project_id="proj-1",
                    audio_asset_id=seed_art.artifact_id,
                    start_seconds=0.5,
                    end_seconds=2.5,
                    instructions="gentle acoustic guitar",
                    lyrics="la la",
                    revision_number=1,
                    seed=7,
                    crossfade_seconds=0.2,
                    vocal_language="en",
                )

            acquires = [b["consumer_kind"] for p, b in sched_calls if p.endswith("/acquire")]
            self.assertEqual(acquires, ["uvr5", "rvc", "ace-step"])
            releases = [(p, b) for p, b in sched_calls if p.endswith("/release")]
            self.assertEqual(len(releases), 3)
            # UVR5 & ACE-Step had receiver_guard proof -> worker_settled=True; RVC had no proof -> worker_settled=False!
            self.assertTrue(releases[0][1]["worker_settled"])
            self.assertFalse(releases[1][1]["worker_settled"])
            self.assertNotIn("reclaim_mode", releases[1][1])
            self.assertTrue(releases[2][1]["worker_settled"])

    def test_scheduler_queue_timeout_surfaces_waiting_gpu_lease_and_aborts_unstarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            seed_art = store.create(
                project_id="proj-1",
                operation="music.generate",
                filename="source.wav",
                media_type="audio/wav",
                payload=_make_wav(1.0),
            )
            t = {"now": 0.0}
            rel_bodies: list[dict] = []

            def sched_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                if request.url.path.endswith("/release"):
                    rel_bodies.append(body)
                    return httpx.Response(200, json={"lease_id": "lease-q1", "state": "released"})
                return httpx.Response(
                    202,
                    json={
                        "lease_id": "lease-q1",
                        "state": "queued",
                        "blocking_reason": "INSUFFICIENT_FREE_VRAM",
                        "blocking_holder_id": "video-mcp",
                    },
                )

            sched = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                queue_seconds=2,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
                monotonic=lambda: t["now"],
                sleep=lambda s: t.__setitem__("now", t["now"] + s),
            )
            executor = MusicExecutor(
                store=store,
                service_urls=ServiceURLs("http://music3", "http://uvr5", "http://rvc", "http://align", "http://mix"),
                client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"status": "ok"}))),
                scheduler=sched,
            )
            with self.assertRaisesRegex(ExecutionError, "waiting_gpu_lease"):
                executor.separate_stems(project_id="proj-1", audio_asset_id=seed_art.artifact_id)
            self.assertEqual(len(rel_bodies), 1)
            self.assertEqual(rel_bodies[0]["reclaim_mode"], "abort_unstarted")

    def test_dual_track_speech_and_music3_interleaving_never_deadlocks(self) -> None:
        """Prove canonical :5056 -> non-blocking K8s Lease order prevents cross-service deadlock (H7)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            shared_api = MemoryApi()
            now_dt = datetime(2026, 11, 15, tzinfo=timezone.utc)
            barrier = threading.Barrier(2, timeout=3.0)

            music_gpu = GpuCoordinator(
                api=shared_api,
                role="music3",
                holder="music3/pod-a",
                deployment="music-minimax-music3",
                enabled=True,
                now=lambda: now_dt,
            )
            speech_gpu = GpuCoordinator(
                api=shared_api,
                role="indextts2",
                holder="indextts2/pod-b",
                deployment="speech-indextts2",
                enabled=True,
                now=lambda: now_dt,
            )

            sched_releases: list[tuple[str, str]] = []
            sched_lock = threading.Lock()

            def sched_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                if request.url.path.endswith("/acquire"):
                    ck = body["consumer_kind"]
                    # Force both threads to acquire :5056 simultaneously before touching K8s Leases
                    barrier.wait()
                    return httpx.Response(
                        200,
                        json={
                            "lease_id": f"lease-{ck}-race",
                            "state": "leased",
                            "fencing_token": 60 if ck == "music3" else 61,
                            "attempt_id": body["attempt_id"],
                            "stage_id": body["stage_id"],
                            "gpu_uuids": ["GPU-00000000-0000-0000-0000-000000000001"],
                            "expires_at": "2099-01-01T00:00:00Z",
                        },
                    )
                if request.url.path.endswith("/release"):
                    lid = request.url.path.split("/")[-2]
                    with sched_lock:
                        sched_releases.append((lid, str(body.get("reclaim_mode") or "")))
                    return httpx.Response(200, json={"lease_id": lid, "state": "released"})
                return httpx.Response(422, json={"error_code": "TOPOLOGY_UNSATISFIABLE", "message": "stop"})

            sched = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            executor = MusicExecutor(
                store=store,
                service_urls=ServiceURLs("http://music3", "http://uvr5", "http://rvc", "http://align", "http://mix"),
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda _r: httpx.Response(
                            200,
                            json={
                                "status": "ok",
                                "runtime_identity": {
                                    "pod_uid": "pod-m3",
                                    "container_id": "cid-m3",
                                    "boot_id": "boot-1",
                                    "visible_gpu_uuids": ["GPU-00000000-0000-0000-0000-000000000001"],
                                },
                            },
                        )
                    )
                ),
                gpu=music_gpu,
                scheduler=sched,
            )

            outcomes: list[str] = []

            def run_music() -> None:
                try:
                    executor.generate(
                        project_id="proj-1",
                        lyrics="[Verse]\nhi",
                        instructions="pop",
                        candidate_number=1,
                        seed=1,
                        max_duration_seconds=5.0,
                    )
                    outcomes.append("music_ok")
                except ExecutionError as exc:
                    outcomes.append(f"music_err:{exc}")

            def run_speech_sim() -> None:
                lease = sched.acquire(
                    consumer_key="uvr5",  # 1-card slice stand-in at :5056
                    task_id="speech-race-1",
                    attempt_id="att-1",
                )
                if not speech_gpu.try_acquire_hold():
                    sched.release(lease["lease_id"], lease["fencing_token"], worker_settled=False, reclaim_mode="abort_unstarted")
                    outcomes.append("speech_contended_rolled_back")
                else:
                    speech_gpu.release()
                    sched.release(lease["lease_id"], lease["fencing_token"], worker_settled=False, reclaim_mode="abort_unstarted")
                    outcomes.append("speech_acquired_and_released")

            t1 = threading.Thread(target=run_music)
            t2 = threading.Thread(target=run_speech_sim)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertFalse(t1.is_alive(), "music thread deadlocked!")
            self.assertFalse(t2.is_alive(), "speech thread deadlocked!")
            self.assertEqual(len(outcomes), 2)
            # Both :5056 leases were cleanly released with zero deadlock
            self.assertEqual(len(sched_releases), 2)

    def test_durable_pending_release_journal_survives_restart_and_verifies_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "gpu_pending_releases.json"
            state = {"l1": "fail_404", "l2": "wrong_id"}
            seen_releases: list[tuple[str, dict]] = []

            def sched_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                if request.url.path.endswith("/release"):
                    lid = request.url.path.split("/")[-2]
                    seen_releases.append((lid, body))
                    mode = state.get(lid, "ok")
                    if mode == "fail_404":
                        return httpx.Response(404, json={"error_code": "LEASE_NOT_FOUND"})
                    if mode == "wrong_id":
                        return httpx.Response(200, json={"lease_id": "other-lease", "state": "released"})
                    if mode == "reclaiming":
                        return httpx.Response(200, json={"lease_id": lid, "state": "reclaiming"})
                    return httpx.Response(200, json={"lease_id": lid, "state": "released"})
                return httpx.Response(
                    200,
                    json={
                        "lease_id": "lease-new-1",
                        "state": "leased",
                        "fencing_token": 99,
                        "attempt_id": body.get("attempt_id", "att-1"),
                        "stage_id": body.get("stage_id", "uvr5_separate"),
                        "gpu_uuids": ["GPU-11111111-2222-3333-4444-555555555555"],
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                )

            c1 = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                pending_releases_path=journal_path,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            with self.assertRaises(SchedulerError):
                c1.release("l1", 10, worker_settled=False, reclaim_mode="abort_unstarted", expected_version=3)
            with self.assertRaises(SchedulerError):
                c1.release("l2", 11, worker_settled=False, reclaim_mode="abort_unstarted", expected_version=4)

            persisted = json.loads(journal_path.read_text(encoding="utf-8"))["pending_releases"]
            self.assertEqual(set(persisted.keys()), {"l1", "l2"})
            self.assertNotIn("expected_version", persisted["l1"])

            # Restart client from disk; l1 still fails (reclaiming), l2 succeeds -> multi-entry flush keeps l1 and clears l2!
            state["l1"] = "reclaiming"
            state["l2"] = "ok"
            c2 = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                pending_releases_path=journal_path,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            self.assertEqual(c2.flush_pending_releases(), 1)
            persisted2 = json.loads(journal_path.read_text(encoding="utf-8"))["pending_releases"]
            self.assertEqual(set(persisted2.keys()), {"l1"})

            # Next acquire flushes l1 once it reaches terminal state
            state["l1"] = "ok"
            lease_doc = c2.acquire(consumer_key="uvr5", task_id="t-after-flush", attempt_id="att-1")
            self.assertEqual(lease_doc["lease_id"], "lease-new-1")
            self.assertEqual(json.loads(journal_path.read_text(encoding="utf-8"))["pending_releases"], {})

            # 400 quarantines pending entry (preserving reclaim_proof, stopping blind auto-flush) until corrected release recovers!
            state["l-400"] = "fail_400"
            orig_handler = sched_handler

            def handler_with_400(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8")) if request.content else {}
                if request.url.path.endswith("/l-400/release") and body.get("reclaim_mode") == "bad_mode":
                    seen_releases.append(("l-400", body))
                    return httpx.Response(400, json={"error_code": "INVALID_REQUEST", "message": "bad mode"})
                return orig_handler(request)

            c_q = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                pending_releases_path=journal_path,
                client=httpx.Client(transport=httpx.MockTransport(handler_with_400)),
            )
            saved_proof = {"reclaimed": True, "active_tasks": 0, "proof_source": "receiver_guard", "token_sealed": True}
            with self.assertRaises(SchedulerError) as q_err:
                c_q.release("l-400", 55, worker_settled=True, reclaim_mode="bad_mode", reclaim_proof=saved_proof)
            self.assertEqual(q_err.exception.code, "INVALID_REQUEST")
            q_entry = c_q.get_pending_release("l-400")
            self.assertIsNotNone(q_entry)
            self.assertTrue(q_entry["_quarantined_400"])  # type: ignore[index]
            self.assertEqual(q_entry["reclaim_proof"], saved_proof)  # type: ignore[index]
            before_count = len(seen_releases)
            self.assertEqual(c_q.flush_pending_releases(), 0)
            self.assertEqual(len(seen_releases), before_count)  # Quarantined entry NOT retried blindly!
            recovered = c_q.release(
                "l-400",
                55,
                worker_settled=True,
                reclaim_mode="warm_idle_settled",
                reclaim_proof=q_entry["reclaim_proof"],  # type: ignore[index]
            )
            self.assertEqual(recovered["state"], "released")
            self.assertIsNone(c_q.get_pending_release("l-400"))

            # Corrupt journal fails closed on startup
            corrupt_journal = Path(tmp) / "corrupt_journal.json"
            corrupt_journal.write_text("{corrupt", encoding="utf-8")
            c_corrupt = SchedulerLeaseClient(
                base_url="http://station-runtime:5056",
                token="tok",
                enabled=True,
                pending_releases_path=corrupt_journal,
                client=httpx.Client(transport=httpx.MockTransport(sched_handler)),
            )
            with self.assertRaises(SchedulerError) as ctx:
                c_corrupt.acquire(consumer_key="uvr5", task_id="t-blocked", attempt_id="att-1")
            self.assertEqual(ctx.exception.code, "JOURNAL_CORRUPTED")


if __name__ == "__main__":
    unittest.main()
