import io
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

import httpx

from verdantflare_music_mcp.artifacts import ArtifactStore
from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs, extract_expected_zip


def wav_bytes(seconds: float, sample_rate: int = 32000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00\x01\x00" * round(seconds * sample_rate))
    return output.getvalue()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
        archive.writestr("manifest.json", "{}")
    return output.getvalue()


class ExecutorTest(unittest.TestCase):
    def test_generate_calls_music3_and_persists_exact_duration(self) -> None:
        requests: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=wav_bytes(90.25), headers={"Content-Type": "audio/wav"})

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            client = httpx.Client(transport=httpx.MockTransport(handler))
            executor = MusicExecutor(store, ServiceURLs("http://music", "http://uvr", "http://rvc", "http://mix"), client)
            records = executor.generate(
                project_id="mengsk-error",
                lyrics="[Verse]\nline",
                instructions="restrained chamber soul",
                candidate_number=1,
                seed=7,
                duration_seconds=90,
            )
            self.assertEqual(requests[0]["max_new_tokens"], 2250)
            self.assertEqual(
                [record.filename for record in records],
                ["Demo_Candidate_1.generated.wav", "Demo_Candidate_1.wav"],
            )
            _, exact = store.read(records[1].artifact_id, "mengsk-error")
            with wave.open(io.BytesIO(exact), "rb") as audio:
                self.assertEqual(audio.getnframes(), 90 * 32000)

    def test_generate_rejects_early_music3_output(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=wav_bytes(89.5))

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            with self.assertRaisesRegex(ExecutionError, "ended before"):
                executor.generate(
                    project_id="mengsk-error",
                    lyrics="lyrics",
                    instructions="instructions",
                    candidate_number=1,
                    seed=7,
                    duration_seconds=90,
                )

    def test_zip_requires_exact_safe_members(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("instrumental.wav", b"instrumental")
            archive.writestr("vocal.wav", b"vocal")
        extracted = extract_expected_zip(
            output.getvalue(),
            {"instrumental.wav": "audio/wav", "vocal.wav": "audio/wav"},
        )
        self.assertEqual(extracted["vocal.wav"], b"vocal")

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../instrumental.wav", b"bad")
        with self.assertRaises(ExecutionError):
            extract_expected_zip(unsafe.getvalue(), {"instrumental.wav": "audio/wav"})

        unexpected = zip_bytes({"instrumental.wav": b"ok", "debug.log": b"not allowed"})
        with self.assertRaisesRegex(ExecutionError, "exact expected files"):
            extract_expected_zip(unexpected, {"instrumental.wav": "audio/wav"})

    def test_stems_conversion_and_master_persist_real_outputs(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/audio/stem-separations":
                return httpx.Response(
                    200,
                    content=zip_bytes(
                        {
                            "instrumental.wav": b"instrumental",
                            "vocal_dry_original.wav": b"original-vocal",
                        }
                    ),
                )
            if request.url.path == "/v1/audio/voice-conversions":
                return httpx.Response(200, content=b"cloned-vocal")
            if request.url.path == "/v1/audio/masters":
                return httpx.Response(
                    200,
                    content=zip_bytes(
                        {
                            "Final_Song_Master.wav": b"master",
                            "Final_Song.mp3": b"mp3",
                            "Final_Song.lrc": b"[00:00.00]line",
                        }
                    ),
                )
            raise AssertionError(request.url.path)

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            source = store.create(
                project_id="mengsk-error",
                operation="music.generate",
                filename="Demo_Selected.wav",
                media_type="audio/wav",
                payload=b"candidate",
            )
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            stems = executor.separate_stems(project_id="mengsk-error", audio_asset_id=source.artifact_id)
            cloned = executor.convert_voice(
                project_id="mengsk-error",
                audio_asset_id=stems[1].artifact_id,
                model_id="mengsk-demo-v1",
                pitch_shift=0,
            )
            final = executor.master(
                project_id="mengsk-error",
                instrumental_asset_id=stems[0].artifact_id,
                vocal_asset_id=cloned[0].artifact_id,
                lyrics_lrc="[00:00.00]line",
                bpm=72,
            )
            self.assertEqual([item.filename for item in final], ["Final_Song_Master.wav", "Final_Song.mp3", "Final_Song.lrc"])
            self.assertEqual(store.read(final[0].artifact_id, "mengsk-error")[1], b"master")

    def test_voice_training_persists_model_outputs(self) -> None:
        model_id = "new-model-v1"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=zip_bytes(
                    {
                        f"{model_id}.pth": b"pth",
                        f"{model_id}.index": b"index",
                        f"{model_id}_validation.wav": b"validation",
                    }
                ),
            )

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            recording = store.create(
                project_id="voice-project",
                operation="asset.import",
                filename="recording.mp3",
                media_type="audio/mpeg",
                payload=b"recording",
            )
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            outputs = executor.train_voice(
                project_id="voice-project",
                audio_asset_id=recording.artifact_id,
                model_id=model_id,
                epochs=200,
                batch_size=4,
            )
            self.assertEqual(len(outputs), 3)
            self.assertEqual(store.read(outputs[2].artifact_id, "voice-project")[1], b"validation")


if __name__ == "__main__":
    unittest.main()
