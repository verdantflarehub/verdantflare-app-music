import io
import json
import re
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import httpx

from verdantflare_music_mcp.artifacts import ArtifactNotFound, ArtifactStore
from verdantflare_music_mcp.duet import (
    DuetLine, build_duet_plan, fit_vocal_to_backing, lyrics_for_voice,
    pcm16_wav, preview_duet, read_duet_plan,
)
from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs


def wav(seconds: float, value: int = 1000, rate: int = 1000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", value) * round(seconds * rate))
    return out.getvalue()


def lines() -> list[DuetLine]:
    return [
        DuetLine(section="Verse", voice="female", text="女声第一句", bars=4),
        DuetLine(section="Verse", voice="male", text="男声第二句", bars=4),
        DuetLine(section="Chorus", voice="both", text="一起唱", bars=4),
    ]


STYLE = "Contemporary anime electronic pop with syncopated drums and sharp synth hooks"


class DuetTest(unittest.TestCase):
    def test_plan_and_role_lyrics(self) -> None:
        plan = build_duet_plan(lines(), bpm=120, style=STYLE, candidate_number=4)
        self.assertEqual(plan["duration_seconds"], 24)
        self.assertEqual([line["start_seconds"] for line in plan["lines"]], [0, 8, 16])
        self.assertEqual(read_duet_plan(json.dumps(plan).encode()), plan)
        self.assertIn("女声第一句\n[Instrumental]\n[Chorus]\n一起唱", lyrics_for_voice(plan, "female"))
        self.assertIn("[Instrumental]\n男声第二句\n[Chorus]\n一起唱", lyrics_for_voice(plan, "male"))

        altered = {**plan, "duration_seconds": 25}
        with self.assertRaisesRegex(ValueError, "does not match"):
            read_duet_plan(json.dumps(altered).encode())
        for invalid in (
            [DuetLine(section="Verse", voice="female", text="a")] * 3,
            [*lines()[:2], DuetLine(section="Chorus", voice="both", text="x", bars=9)],
        ):
            with self.assertRaises(ValueError):
                build_duet_plan(invalid, bpm=120, style=STYLE, candidate_number=4)
        with self.assertRaisesRegex(ValueError, "integer"):
            build_duet_plan(lines(), bpm=120.5, style=STYLE, candidate_number=4)

    def test_masking_and_preview_keep_distinct_full_length_tracks(self) -> None:
        plan = build_duet_plan(lines(), bpm=120, style=STYLE, candidate_number=1)
        backing = wav(24, 200)
        female = fit_vocal_to_backing(backing, wav(24, 1000), plan, "female")
        male = fit_vocal_to_backing(backing, wav(24, 1500), plan, "male")
        self.assertEqual(pcm16_wav(female)[0].nframes, 24000)
        self.assertEqual(pcm16_wav(male)[0].nframes, 24000)
        female_frames = pcm16_wav(female)[1]
        male_frames = pcm16_wav(male)[1]
        self.assertEqual(struct.unpack_from("<h", female_frames, 12000 * 2)[0], 0)
        self.assertEqual(struct.unpack_from("<h", male_frames, 4000 * 2)[0], 0)
        self.assertEqual(struct.unpack_from("<h", female_frames, 20000 * 2)[0], 1000)
        self.assertEqual(struct.unpack_from("<h", male_frames, 20000 * 2)[0], 1500)
        self.assertEqual(pcm16_wav(preview_duet(backing, female, male))[0].nframes, 24000)
        with self.assertRaisesRegex(ValueError, "independent"):
            preview_duet(backing, female, female)
        with self.assertRaisesRegex(ValueError, "no audible content"):
            fit_vocal_to_backing(backing, wav(24, 0), plan, "female")
        with self.assertRaisesRegex(ValueError, "duration differs"):
            fit_vocal_to_backing(backing, wav(23), plan, "female")

    def test_wav_validation_rejects_truncated_and_wrong_format(self) -> None:
        with self.assertRaises(ValueError):
            pcm16_wav(wav(1)[:-2])
        with self.assertRaises(ValueError):
            pcm16_wav(b"not wav")

    def test_preflight_and_three_stage_generation(self) -> None:
        submitted = []
        fail_query = [False]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"code": 200, "data": {"models": [{"name": "acestep-v15-base", "is_loaded": True}]}})
            if request.url.path == "/release_task":
                body = request.content.decode("latin1")
                submitted.append(body)
                return httpx.Response(200, json={"code": 200, "data": {"task_id": f"task-{len(submitted)}"}})
            if request.url.path == "/query_result":
                if fail_query[0]:
                    fail_query[0] = False
                    return httpx.Response(503, json={"error": "temporary"})
                task_id = json.loads(request.content)["task_id_list"][0]
                return httpx.Response(200, json={"code": 200, "data": [{
                    "task_id": task_id, "status": 1,
                    "result": json.dumps([{"file": f"/audio/{task_id}.wav"}]),
                }]})
            if request.url.path.startswith("/audio/"):
                index = int(request.url.path.split("-")[1].split(".")[0])
                return httpx.Response(200, content=wav(24, 200 if index == 1 else index * 1000))
            raise AssertionError(request.url)

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            client = httpx.Client(transport=httpx.MockTransport(handler))
            executor = MusicExecutor(store, ServiceURLs(
                "http://music", "http://uvr", "http://rvc", "http://align", "http://mix",
                music_editor="http://ace",
            ), client)
            self.assertEqual(executor.preflight(workflow="duet_generate")["status"], "ready")
            plan = executor.duet_plan(project_id="duet", lines=lines(), bpm=120, style=STYLE, candidate_number=1)[0]
            backing_outputs = executor.duet_instrumental(project_id="duet", plan_asset_id=plan.artifact_id)
            backing = backing_outputs[1]
            female = executor.duet_vocal(project_id="duet", plan_asset_id=plan.artifact_id,
                                          instrumental_asset_id=backing.artifact_id, voice="female")
            male = executor.duet_vocal(project_id="duet", plan_asset_id=plan.artifact_id,
                                        instrumental_asset_id=backing.artifact_id, voice="male")
            preview = executor.duet_preview(project_id="duet", plan_asset_id=plan.artifact_id,
                                             instrumental_asset_id=backing.artifact_id,
                                             female_asset_id=female[2].artifact_id,
                                             male_asset_id=male[2].artifact_id)[0]
            self.assertEqual(len(submitted), 3)
            self.assertIn("task_type=text2music", submitted[0])
            self.assertIn("inference_steps=50", submitted[0])
            self.assertIn("batch_size=1", submitted[0])
            self.assertIn("text2music", submitted[0])
            self.assertIn("lego", submitted[1])
            self.assertIn("src_audio", submitted[1])
            self.assertIn("track_name", submitted[1])
            self.assertEqual(backing_outputs[0].operation, "duet.task")
            resumed = executor.duet_instrumental(
                project_id="duet", plan_asset_id=plan.artifact_id,
                resume_task_asset_id=backing_outputs[0].artifact_id,
            )
            self.assertEqual(resumed[0].artifact_id, backing_outputs[0].artifact_id)
            self.assertEqual(len(submitted), 3)
            with self.assertRaisesRegex(ValueError, "does not match"):
                executor.duet_instrumental(
                    project_id="duet", plan_asset_id=plan.artifact_id, seed=8,
                    resume_task_asset_id=backing_outputs[0].artifact_id,
                )
            self.assertNotEqual(store.read(female[2].artifact_id, "duet")[1],
                                store.read(male[2].artifact_id, "duet")[1])
            self.assertEqual(preview.filename, "Duet_Candidate_1.wav")
            second_plan = executor.duet_plan(project_id="duet", lines=lines(), bpm=120,
                                             style=STYLE, candidate_number=2)[0]
            fail_query[0] = True
            with self.assertRaisesRegex(ExecutionError, "resume with Artifact") as raised:
                executor.duet_instrumental(project_id="duet", plan_asset_id=second_plan.artifact_id)
            receipt_id = re.search(r"art_[0-9a-f]{32}", str(raised.exception)).group()
            self.assertEqual(store.get(receipt_id, "duet").operation, "duet.task")
            executor.duet_instrumental(project_id="duet", plan_asset_id=second_plan.artifact_id,
                                       resume_task_asset_id=receipt_id)
            self.assertEqual(len(submitted), 4)
            with self.assertRaises(ArtifactNotFound):
                executor.duet_vocal(project_id="other", plan_asset_id=plan.artifact_id,
                                    instrumental_asset_id=backing.artifact_id, voice="female")
            self.assertEqual(len(submitted), 4)

    def test_failed_duration_keeps_instrumental_and_never_calls_music3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            executor = MusicExecutor(store, ServiceURLs(
                "http://music", "http://uvr", "http://rvc", "http://align", "http://mix",
                music_editor="http://ace",
            ), httpx.Client())
            plan = executor.duet_plan(project_id="duet", lines=lines(), bpm=120, style=STYLE, candidate_number=1)[0]
            receipt = store.create(project_id="duet", operation="duet.task", filename="Duet_Task.json",
                                   media_type="application/json", payload=b"{}")
            with mock.patch.object(executor, "_ace_duet_audio", return_value=(wav(10), receipt)):
                with self.assertRaisesRegex(ExecutionError, "Artifact (art_[0-9a-f]{32})") as raised:
                    executor.duet_instrumental(project_id="duet", plan_asset_id=plan.artifact_id)
            artifact_id = raised.exception.args[0].split("Artifact ")[1]
            self.assertEqual(pcm16_wav(store.read(artifact_id, "duet")[1])[0].nframes, 10000)
            self.assertEqual(store.get(artifact_id, "duet").operation, "duet.instrumental.rejected")
            with self.assertRaisesRegex(ValueError, "does not match"):
                executor.duet_vocal(project_id="duet", plan_asset_id=plan.artifact_id,
                                    instrumental_asset_id=artifact_id, voice="female")
            self.assertEqual(executor.service_urls.music3, "http://music")

    def test_unconfigured_and_turbo_backend_block_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for urls in (
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix",
                            music_editor="http://ace", duet_model="acestep-v15-turbo"),
            ):
                executor = MusicExecutor(ArtifactStore(Path(directory)), urls, httpx.Client())
                plan = executor.duet_plan(project_id="duet", lines=lines(), bpm=120, style=STYLE,
                                          candidate_number=2)[0]
                with self.assertRaises(ExecutionError), mock.patch.object(executor, "_post_json") as post:
                    executor.duet_instrumental(project_id="duet", plan_asset_id=plan.artifact_id)
                post.assert_not_called()

    def test_preflight_requires_loaded_base_model(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"code": 200, "data": {
                    "models": [{"name": "acestep-v15-base", "is_loaded": False}],
                }})
            raise AssertionError(request.url)

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(ArtifactStore(Path(directory)), ServiceURLs(
                "http://music", "http://uvr", "http://rvc", "http://align", "http://mix",
                music_editor="http://ace",
            ), httpx.Client(transport=httpx.MockTransport(handler)))
            result = executor.preflight(workflow="duet_generate")
            self.assertEqual(result["status"], "blocked")
            self.assertFalse(result["duet_generation"]["ready"])
            self.assertIn("ACE-Step duet base model is not loaded", result["blocking_conditions"])


if __name__ == "__main__":
    unittest.main()
