import hashlib
import io
import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

import httpx

from verdantflare_music_mcp.artifacts import ArtifactError, ArtifactStore, MAX_ARTIFACT_BYTES
from verdantflare_music_mcp.executor import (
    ExecutionError,
    MusicExecutor,
    ServiceURLs,
    extract_expected_zip,
    validate_aligned_lrc,
)


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
    def test_import_asset_downloads_allowlisted_s3_object_and_verifies_integrity(self) -> None:
        payload = b"customer-audio"
        requested_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_urls.append(str(request.url))
            return httpx.Response(200, content=payload, headers={"Content-Length": str(len(payload))})

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
                frozenset({"https://cache.ali.wodcloud.com"}),
            )
            records = executor.import_asset(
                project_id="mengsk-cover",
                source_url="https://cache.ali.wodcloud.com/vscode/customer/source.mp3?signature=redacted",
                filename="source.mp3",
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(len(requested_urls), 1)
            self.assertEqual(records[0].operation, "asset.import")
            self.assertEqual(records[0].media_type, "audio/mpeg")
            self.assertEqual(store.read(records[0].artifact_id, "mengsk-cover")[1], payload)

    def test_import_asset_rejects_untrusted_origin_redirect_size_and_hash(self) -> None:
        payload = b"customer-audio"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("redirect.mp3"):
                return httpx.Response(302, headers={"Location": "https://evil.example/source.mp3"})
            if request.url.path.endswith("large.mp3"):
                return httpx.Response(200, content=b"x", headers={"Content-Length": str(MAX_ARTIFACT_BYTES + 1)})
            return httpx.Response(200, content=payload)

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
                frozenset({"https://cache.ali.wodcloud.com"}),
            )
            arguments = {
                "project_id": "mengsk-cover",
                "filename": "source.mp3",
                "expected_sha256": hashlib.sha256(payload).hexdigest(),
            }
            with self.assertRaisesRegex(ValueError, "origin is not allowed"):
                executor.import_asset(source_url="https://evil.example/source.mp3", **arguments)
            with self.assertRaisesRegex(ExecutionError, "HTTP 302"):
                executor.import_asset(
                    source_url="https://cache.ali.wodcloud.com/vscode/customer/redirect.mp3",
                    **arguments,
                )
            with self.assertRaisesRegex(ExecutionError, "between 1 byte and 1 GiB"):
                executor.import_asset(
                    source_url="https://cache.ali.wodcloud.com/vscode/customer/large.mp3",
                    **arguments,
                )
            with self.assertRaisesRegex(ArtifactError, "SHA-256"):
                executor.import_asset(
                    source_url="https://cache.ali.wodcloud.com/vscode/customer/source.mp3",
                    **(arguments | {"expected_sha256": "0" * 64}),
                )

    def test_generate_calls_music3_and_preserves_natural_duration(self) -> None:
        requests: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, content=wav_bytes(90.25), headers={"Content-Type": "audio/wav"})

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory), "https://music.example")
            client = httpx.Client(transport=httpx.MockTransport(handler))
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                client,
            )
            records = executor.generate(
                project_id="mengsk-error",
                lyrics="[Verse]\nline",
                instructions="restrained chamber soul",
                candidate_number=1,
                seed=7,
                max_duration_seconds=90,
            )
            self.assertEqual(requests[0]["max_new_tokens"], 2250)
            self.assertEqual([record.filename for record in records], ["Demo_Candidate_1.wav"])
            _, candidate = store.read(records[0].artifact_id, "mengsk-error")
            with wave.open(io.BytesIO(candidate), "rb") as audio:
                self.assertEqual(audio.getnframes(), round(90.25 * 32000))

    def test_generate_accepts_early_natural_music3_output(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=wav_bytes(89.5))

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            records = executor.generate(
                project_id="mengsk-error",
                lyrics="lyrics",
                instructions="instructions",
                candidate_number=1,
                seed=7,
                max_duration_seconds=90,
            )
            _, candidate = executor.store.read(records[0].artifact_id, "mengsk-error")
            with wave.open(io.BytesIO(candidate), "rb") as audio:
                self.assertEqual(audio.getnframes(), round(89.5 * 32000))

    def test_generate_rejects_invalid_music3_wav(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not-a-wav")

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            with self.assertRaisesRegex(ExecutionError, "invalid WAV"):
                executor.generate(
                    project_id="mengsk-error",
                    lyrics="lyrics",
                    instructions="instructions",
                    candidate_number=1,
                    seed=7,
                    max_duration_seconds=90,
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
            if request.url.path == "/v1/lyrics/alignments":
                return httpx.Response(200, content="[00:01.000]第一句\n".encode())
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
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            stems = executor.separate_stems(project_id="mengsk-error", audio_asset_id=source.artifact_id)
            cloned = executor.convert_voice(
                project_id="mengsk-error",
                audio_asset_id=stems[1].artifact_id,
                model_id="mengsk-demo-v1",
                pitch_shift=0,
            )
            aligned = executor.align_lyrics(
                project_id="mengsk-error",
                vocal_asset_id=cloned[0].artifact_id,
                lyrics="第一句",
                language="zh",
            )
            self.assertEqual(store.read(aligned[0].artifact_id, "mengsk-error")[1], "[00:01.000]第一句\n".encode())
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
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
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

    def test_aligned_lrc_must_preserve_lines_and_strict_timestamps(self) -> None:
        valid = "[00:01.000]第一句\n[00:02.250]第二句\n".encode()
        self.assertEqual(validate_aligned_lrc(valid, "第一句\n第二句"), valid.decode())
        invalid = (
            "[00:01.000]第一句\n".encode(),
            "[00:01.000]第一句\n[00:01.000]第二句\n".encode(),
            "[00:01.000]第一句\n[00:02.000]改写\n".encode(),
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ExecutionError):
                validate_aligned_lrc(payload, "第一句\n第二句")


if __name__ == "__main__":
    unittest.main()
