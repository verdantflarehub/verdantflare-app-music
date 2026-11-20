"""Cold startup lease tests: virtual time, real heartbeat thread, no GPU."""
from __future__ import annotations

import contextlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs
from verdantflare_music_mcp.gpu_lease import GpuCoordinator, MemoryApi, SchedulerError


class HeartbeatTicks:
    """Advance one interval and wait until that heartbeat iteration has finished."""

    def __init__(self):
        self.condition = threading.Condition()
        self.requested = 0
        self.completed = 0
        self.dispatched = 0
        self.stopped = False

    def wait(self, interval):
        with self.condition:
            self.completed = self.dispatched
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.stopped or self.requested > self.dispatched)
            if self.stopped:
                return True
            self.dispatched += 1
            return False

    def is_set(self):
        with self.condition:
            return self.stopped

    def set(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def pulse(self):
        with self.condition:
            self.requested += 1
            self.condition.notify_all()

    def flush(self):
        with self.condition:
            if not self.condition.wait_for(lambda: self.completed == self.requested, timeout=2):
                raise AssertionError("heartbeat did not run during startup")

    def step(self):
        self.pulse()
        self.flush()


class ObservedLock:
    """Expose attempted acquisition so bind/heartbeat overlap is deterministic."""

    def __init__(self):
        self.lock = threading.Lock()
        self.observe = False
        self.heartbeat_attempt = threading.Event()
        self.after_main_exit = None

    def __enter__(self):
        if self.observe and threading.current_thread().name.startswith("music-sched-hb-"):
            self.heartbeat_attempt.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()
        if threading.current_thread() is threading.main_thread() and self.after_main_exit:
            callback, self.after_main_exit = self.after_main_exit, None
            callback()


class ColdStartHeartbeatTest(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, *, legacy=False, warm=False):
        with tempfile.TemporaryDirectory() as tmp:
            ticks = HeartbeatTicks()
            lock = ObservedLock()
            state = {"time": 0, "expires": 90, "version": 1}
            identity = {"pod_uid": "pod", "container_id": "container", "boot_id": "boot",
                        "visible_gpu_uuids": ["gpu"]}
            lease = {"lease_id": "lease", "fencing_token": 7, "version": 1}
            if warm:
                lease.update(pod_uid="pod", gpu_uuids=["gpu"])
            scheduler = Mock(enabled=True, ttl_seconds=90)
            scheduler.acquire_or_wait.return_value = lease
            scheduler.build_grant_headers.return_value = {"X-Test-Grant": "granted"}

            def heartbeat(lease_id, fence):
                self.assertEqual((lease_id, fence), ("lease", 7))
                self.assertLess(state["time"], state["expires"])
                state["version"] += 1
                state["expires"] = state["time"] + 90
                return {**lease, "version": state["version"]}

            def bind(lease_id, fence, **kwargs):
                self.assertLess(state["time"], state["expires"])
                self.assertEqual(kwargs["expected_version"], state["version"])
                state["version"] += 1
                return {**lease, "version": state["version"], "pod_uid": "pod", "gpu_uuids": ["gpu"]}

            scheduler.heartbeat.side_effect = heartbeat
            scheduler.bind_target.side_effect = bind
            gpu = GpuCoordinator(MemoryApi(), "", "music-minimax-music3", enabled=legacy)
            gpu.replicas = Mock(return_value=int(warm))
            gpu.replicas_for = Mock(return_value=int(warm))
            gpu.try_acquire_hold = Mock(return_value=True)
            gpu.scale_to = Mock()
            gpu.scale_deployment_to = Mock()
            gpu.renew = Mock()
            gpu.release = Mock()
            gpu.stop_deployment_and_wait = Mock()
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={
                "reclaim_proof": {"proof_source": "receiver_guard", "token_sealed": True,
                                  "active_tasks": 0, "upstream_alive": True, "resident_procs": []},
            }))) as client:
                executor = MusicExecutor(ArtifactStore(Path(tmp)), ServiceURLs("m", "u", "r", "l", "a"),
                                         client=client, gpu=gpu, scheduler=scheduler)
                executor._probe_runtime_identity = Mock(return_value=identity)
                executor._wait_music3 = Mock()
                executor._wait_service_ready = Mock()
                thread_api = SimpleNamespace(Event=lambda: ticks, Lock=lambda: lock, Thread=threading.Thread)
                with patch("verdantflare_music_mcp.executor.threading", thread_api):
                    yield SimpleNamespace(executor=executor, scheduler=scheduler, gpu=gpu, ticks=ticks,
                                          state=state, lock=lock, bind=bind, heartbeat=heartbeat)
                self.assertTrue(ticks.is_set())

    def test_cold_start_outlives_ttl_and_binds_latest_version(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), self.fixture(legacy=legacy) as f:
                def wait(*args):
                    for _ in range(4):
                        f.state["time"] += 30
                        f.ticks.step()
                f.executor._wait_music3.side_effect = wait
                f.executor._wait_service_ready.side_effect = wait
                with f.executor._hold_consumer_gpu("music3", "http://worker", use_legacy_mirror=legacy) as (headers, _):
                    self.assertEqual(headers, {"X-Test-Grant": "granted"})
                    self.assertEqual(f.state["time"], 120)
                self.assertEqual(f.scheduler.heartbeat.call_count, 4)
                self.assertEqual(f.scheduler.bind_target.call_args.kwargs["expected_version"], 5)
                self.assertEqual(f.scheduler.release.call_args.kwargs["expected_version"], 6)
                self.assertEqual(f.gpu.renew.call_count, 4 if legacy else 0)

    def test_heartbeat_failure_blocks_bind_and_inference_even_after_recovery(self):
        for recover in (False, True):
            with self.subTest(recover=recover), self.fixture() as f:
                def wait(*args):
                    f.scheduler.heartbeat.side_effect = SchedulerError("FENCING_TOKEN_REVOKED", status_code=409)
                    f.ticks.step()
                    if recover:
                        f.scheduler.heartbeat.side_effect = f.heartbeat
                        f.ticks.step()
                f.executor._wait_service_ready.side_effect = wait
                with self.assertRaisesRegex(ExecutionError, "gpu_lease_heartbeat_failed"):
                    with f.executor._hold_consumer_gpu("music3", "http://worker"):
                        self.fail("invalid lease admitted inference")
                f.scheduler.bind_target.assert_not_called()
                f.scheduler.build_grant_headers.assert_not_called()
                self.assertEqual(f.scheduler.release.call_args.kwargs["reclaim_mode"], "abort_unstarted")
                self.assertFalse(f.scheduler.release.call_args.kwargs["worker_settled"])
                f.gpu.stop_deployment_and_wait.assert_not_called()

    def test_identity_discovery_is_covered_by_heartbeat(self):
        with self.fixture() as f:
            identity = f.executor._probe_runtime_identity.return_value
            def probe(*args):
                for _ in range(4):
                    f.state["time"] += 30
                    f.ticks.step()
                return identity
            f.executor._probe_runtime_identity.side_effect = probe
            with f.executor._hold_consumer_gpu("music3", "http://worker"):
                pass
            self.assertEqual(f.scheduler.bind_target.call_args.kwargs["expected_version"], 5)

    def test_bind_serializes_a_concurrent_heartbeat(self):
        with self.fixture() as f:
            def bind(*args, **kwargs):
                f.lock.observe = True
                f.ticks.pulse()
                self.assertTrue(f.lock.heartbeat_attempt.wait(2))
                f.scheduler.heartbeat.assert_not_called()
                return f.bind(*args, **kwargs)
            f.scheduler.bind_target.side_effect = bind
            with f.executor._hold_consumer_gpu("music3", "http://worker"):
                f.ticks.flush()
            self.assertEqual(f.scheduler.release.call_args.kwargs["expected_version"], 3)

    def test_warm_bound_worker_does_not_rebind_or_scale(self):
        with self.fixture(warm=True) as f:
            with f.executor._hold_consumer_gpu("music3", "http://worker"):
                f.ticks.step()
            f.scheduler.bind_target.assert_not_called()
            f.gpu.scale_deployment_to.assert_not_called()
            f.executor._wait_service_ready.assert_not_called()
            self.assertEqual(f.scheduler.release.call_args.kwargs["expected_version"], 2)

    def test_heartbeat_failure_after_binding_still_blocks_grant(self):
        with self.fixture() as f:
            def bind(*args, **kwargs):
                result = f.bind(*args, **kwargs)
                f.scheduler.heartbeat.side_effect = SchedulerError("FENCING_TOKEN_REVOKED", status_code=409)
                f.lock.after_main_exit = f.ticks.step
                return result
            f.scheduler.bind_target.side_effect = bind
            with self.assertRaisesRegex(ExecutionError, "gpu_lease_heartbeat_failed"):
                with f.executor._hold_consumer_gpu("music3", "http://worker"):
                    self.fail("revoked bound lease admitted inference")
            f.scheduler.bind_target.assert_called_once()
            f.scheduler.build_grant_headers.assert_not_called()
            # A bound worker still needs the original receiver/exit cleanup.
            f.gpu.stop_deployment_and_wait.assert_called_once()
            self.assertTrue(f.scheduler.release.call_args.kwargs["worker_settled"])

    def test_startup_failure_stops_heartbeat_and_aborts_without_inference(self):
        with self.fixture() as f:
            def wait(*args):
                f.ticks.step()
                raise ExecutionError("model_readiness_failed")
            f.executor._wait_service_ready.side_effect = wait
            with self.assertRaisesRegex(ExecutionError, "model_readiness_failed"):
                with f.executor._hold_consumer_gpu("music3", "http://worker"):
                    self.fail("unready worker admitted inference")
            f.scheduler.build_grant_headers.assert_not_called()
            self.assertEqual(f.scheduler.release.call_args.kwargs["expected_version"], 2)
            self.assertEqual(f.scheduler.release.call_args.kwargs["reclaim_mode"], "abort_unstarted")


if __name__ == "__main__":
    unittest.main()
