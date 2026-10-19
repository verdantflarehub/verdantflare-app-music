from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import httpx

from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs
from verdantflare_music_mcp.gpu_lease import GpuCoordinator, LeaseBusy, MemoryApi, replicas_from_scale


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.now = datetime(2026, 10, 19, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.now += timedelta(seconds=seconds)


def coordinator(role: str, api: MemoryApi, clock: Clock, deployment: str | None = None) -> GpuCoordinator:
    return GpuCoordinator(
        api=api,
        role=role,
        holder=f"{role}/test",
        deployment=deployment,
        enabled=True,
        idle_seconds=600,
        queue_seconds=5,
        ready_seconds=1,
        lease_seconds=90,
        now=lambda: clock.now,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


class GpuLeaseTest(unittest.TestCase):
    def test_omitted_scale_replicas_means_zero(self) -> None:
        self.assertEqual(replicas_from_scale({"spec": {}}), 0)
        self.assertEqual(replicas_from_scale({"spec": {"replicas": 1}}), 1)
    def test_music_waits_for_h3_then_acquires_when_expired(self) -> None:
        api = MemoryApi()
        clock = Clock()
        h3 = coordinator("h3", api, clock)
        music = coordinator("music3", api, clock, "music-minimax-music3-api")
        h3.try_acquire()
        with self.assertRaises(LeaseBusy):
            music.acquire_for_queue()
        clock.now += timedelta(seconds=120)
        clock.t = 0
        music.acquire_for_queue()
        self.assertEqual(api.get_lease("gpu-task-music3")["spec"]["holderIdentity"], "music3/test")
        self.assertIsNone(music.peer_holder())

    def test_h3_does_not_exclude_indextts2(self) -> None:
        api = MemoryApi()
        clock = Clock()
        speech = coordinator("indextts2", api, clock)
        video = coordinator("h3", api, clock)
        self.assertTrue(speech.try_acquire())
        self.assertTrue(video.try_acquire())
        self.assertIsNone(video.peer_holder())

    def test_conflict_releases_own_lease(self) -> None:
        api = MemoryApi()
        clock = Clock()
        video = coordinator("h3", api, clock)
        music = coordinator("music3", api, clock)
        video.try_acquire()
        self.assertFalse(music.try_acquire())
        self.assertEqual(api.get_lease("gpu-task-music3")["spec"]["holderIdentity"], "")

    def test_scale_up_and_idle_scale_down(self) -> None:
        api = MemoryApi()
        clock = Clock()
        music = coordinator("music3", api, clock, "music-minimax-music3-api")
        music.acquire_for_queue()
        music.scale_to(1)
        self.assertEqual(api.get_scale("music-minimax-music3-api"), 1)
        music.release()
        music.maybe_scale_down()
        self.assertEqual(api.get_scale("music-minimax-music3-api"), 1)
        clock.t += 600
        music.maybe_scale_down()
        self.assertEqual(api.get_scale("music-minimax-music3-api"), 0)

    def test_preflight_distinguishes_scaled_to_zero_without_blocking_wake(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        api = MemoryApi()
        clock = Clock()
        gpu = coordinator("music3", api, clock, "music-minimax-music3-api")
        executor = MusicExecutor(
            store=None,
            service_urls=ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            gpu=gpu,
        )
        result = executor.preflight(workflow="generate", voice_source="generated_voice")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["checks"][0]["reason"], "scaled_to_zero")
        self.assertTrue(result["checks"][0]["wake_on_task"])
        self.assertFalse(result["layers"]["backend_ready"])
        self.assertTrue(result["layers"]["tool_registered"])
        self.assertEqual(result["blocking_conditions"], [])

    def test_generate_wakes_backend_and_releases(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return httpx.Response(200, content=wav())

        api = MemoryApi()
        clock = Clock()
        gpu = coordinator("music3", api, clock, "music-minimax-music3-api")
        executor = MusicExecutor(
            store=FakeStore(),
            service_urls=ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            gpu=gpu,
        )
        executor._wait_music3 = lambda: seen.append("wait")
        records = executor.generate(
            project_id="proj",
            lyrics="lyrics",
            instructions="instructions",
            candidate_number=1,
            seed=1,
            max_duration_seconds=10,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(api.get_scale("music-minimax-music3-api"), 1)
        self.assertEqual(api.get_lease("gpu-task-music3")["spec"]["holderIdentity"], "")
        self.assertIn("/v1/audio/speech", seen)


def wav() -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(b"\x00\x00" * 8)
    return buffer.getvalue()


class FakeStore:
    def create(self, **kwargs):
        return kwargs


class PreflightFailureTest(unittest.TestCase):
    def test_http_status_is_blocking(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "loading"})

        executor = MusicExecutor(
            store=None,
            service_urls=ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        result = executor.preflight(workflow="generate")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"][0]["reason"], "http_status")
        self.assertEqual(result["checks"][0]["http_status"], 503)
        self.assertIn("HTTP 503", result["blocking_conditions"][0])


if __name__ == "__main__":
    unittest.main()
