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


def wav_bytes(seconds: float, sample_rate: int = 32000, sample_value: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        frame = sample_value.to_bytes(2, "little", signed=True) * 2
        audio.writeframes(frame * round(seconds * sample_rate))
    return output.getvalue()


def mono_wav_bytes(seconds: float, sample_rate: int = 40000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00" * round(seconds * sample_rate))
    return output.getvalue()


def voice_preparation_zip(source_count: int) -> bytes:
    segment_output = io.BytesIO()
    with zipfile.ZipFile(segment_output, "w") as archive:
        for number in range(1, source_count + 1):
            archive.writestr(f"{number:02d}-001.wav", mono_wav_bytes(0.1))
        archive.writestr("manifest.json", "{}")
    report = {
        "schema_version": 1,
        "automatic_status": "passed",
        "review_required": True,
        "sources": [{} for _ in range(source_count)],
    }
    files = {
        **{f"prepared-{number:02d}.wav": mono_wav_bytes(0.1) for number in range(1, source_count + 1)},
        "voice-training.wav": mono_wav_bytes(0.1 * source_count),
        "voice-segments.zip": segment_output.getvalue(),
        "voice-preparation-report.json": json.dumps(report).encode(),
    }
    return zip_bytes(files)


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

    def test_preflight_checks_services_exact_voice_model_and_redraw(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/voice-models":
                return httpx.Response(
                    200,
                    json={"data": [{"id": "mengsk-comfort-v2-20260901", "has_index": True}]},
                )
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            raise AssertionError(request.url.path)

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs(
                    "http://music",
                    "http://uvr",
                    "http://rvc",
                    "http://align",
                    "http://mix",
                    "http://editor",
                ),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            result = executor.preflight(
                workflow="full_song",
                voice_source="approved_model",
                voice_model_id="mengsk-comfort-v2-20260901",
                require_local_redraw=True,
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(result["voice_source"], "approved_model")
            self.assertEqual(result["voice_model"], {"model_id": "mengsk-comfort-v2-20260901", "installed": True})
            self.assertEqual(result["local_redraw"]["ready"], True)

    def test_preflight_reports_missing_model_and_unconfigured_redraw(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/voice-models":
                return httpx.Response(200, json={"data": []})
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            raise AssertionError(request.url.path)

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            result = executor.preflight(
                workflow="full_song",
                voice_source="approved_model",
                voice_model_id="missing-model",
                require_local_redraw=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("voice model missing-model is not installed", result["blocking_conditions"])
            self.assertIn("music_editor is not configured", result["blocking_conditions"])

    def test_preflight_supports_recording_training_before_model_exists(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            raise AssertionError(request.url.path)

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            result = executor.preflight(
                workflow="full_song",
                voice_source="authorized_recordings",
            )
            self.assertEqual(result["status"], "ready")
            self.assertIsNone(result["voice_model"])
            self.assertIn("rerun preflight", result["guidance"])

    def test_preflight_generated_voice_does_not_require_rvc(self) -> None:
        requested_hosts: set[str] = set()

        def handler(request: httpx.Request) -> httpx.Response:
            requested_hosts.add(request.url.host)
            return httpx.Response(200, json={"status": "ok"})

        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            result = executor.preflight(workflow="full_song", voice_source="generated_voice")
            self.assertEqual(result["status"], "ready")
            self.assertNotIn("rvc", requested_hosts)
            self.assertNotIn("uvr", requested_hosts)

    def test_preflight_requires_explicit_authorized_recordings_for_training(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executor = MusicExecutor(
                ArtifactStore(Path(directory)),
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            )
            with self.assertRaisesRegex(ValueError, "authorized_recordings"):
                executor.preflight(workflow="voice_training")

    def test_redraw_calls_conditioned_editor_and_persists_full_song(self) -> None:
        redrawn = wav_bytes(30, sample_rate=48000, sample_value=1000)
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            self.assertEqual(request.headers.get("authorization"), "Bearer editor-secret")
            if request.url.path == "/release_task":
                body = request.content.decode("latin1")
                for expected in (
                    "repaint",
                    "explicit",
                    "balanced",
                    "0.6",
                    "12.0",
                    "20.0",
                    "repair the ending",
                    "0.75",
                    "17",
                ):
                    self.assertIn(expected, body)
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "error": None,
                        "data": {"task_id": "task-1", "status": "queued"},
                    },
                )
            if request.url.path == "/query_result":
                self.assertEqual(json.loads(request.content), {"task_id_list": ["task-1"]})
                return httpx.Response(
                    200,
                    json={
                        "code": 200,
                        "error": None,
                        "data": [
                            {
                                "task_id": "task-1",
                                "status": 1,
                                "result": json.dumps([{"file": "/v1/audio?path=result.wav"}]),
                            }
                        ],
                    },
                )
            if request.url.path == "/v1/audio":
                return httpx.Response(
                    200,
                    content=redrawn,
                    headers={"Content-Type": "audio/wav"},
                )
            raise AssertionError(request.url.path)

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            source = store.create(
                project_id="redraw-project",
                operation="music.generate",
                filename="Demo_Selected.wav",
                media_type="audio/wav",
                payload=wav_bytes(30),
            )
            executor = MusicExecutor(
                store,
                ServiceURLs(
                    "http://music",
                    "http://uvr",
                    "http://rvc",
                    "http://align",
                    "http://mix",
                    "http://editor",
                    music_editor_bearer_token="editor-secret",
                ),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            records = executor.redraw(
                project_id="redraw-project",
                audio_asset_id=source.artifact_id,
                start_seconds=12.0,
                end_seconds=20.0,
                instructions="repair the ending",
                lyrics="",
                revision_number=1,
                seed=17,
                crossfade_seconds=0.75,
                preservation_mode="balanced",
                edit_strength=0.6,
            )
            self.assertEqual(records[0].filename, "Demo_Redraw_1.wav")
            _, persisted = store.read(records[0].artifact_id, "redraw-project")
            with wave.open(io.BytesIO(persisted), "rb") as result_audio:
                self.assertEqual(result_audio.getframerate(), 32000)
                self.assertEqual(result_audio.getnframes(), 30 * 32000)
                result_frames = result_audio.readframes(result_audio.getnframes())
            source_frames = b"\x01\x00\x01\x00" * (30 * 32000)
            frame_bytes = 4
            self.assertEqual(result_frames[: 12 * 32000 * frame_bytes], source_frames[: 12 * 32000 * frame_bytes])
            self.assertEqual(result_frames[20 * 32000 * frame_bytes :], source_frames[20 * 32000 * frame_bytes :])
            self.assertNotEqual(
                result_frames[13 * 32000 * frame_bytes : 19 * 32000 * frame_bytes],
                source_frames[13 * 32000 * frame_bytes : 19 * 32000 * frame_bytes],
            )
            self.assertEqual(requested_paths, ["/release_task", "/query_result", "/v1/audio"])

    def test_redraw_requires_configured_editor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            source = store.create(
                project_id="redraw-project",
                operation="music.generate",
                filename="Demo_Selected.wav",
                media_type="audio/wav",
                payload=wav_bytes(5),
            )
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            )
            with self.assertRaisesRegex(ExecutionError, "not configured"):
                executor.redraw(
                    project_id="redraw-project",
                    audio_asset_id=source.artifact_id,
                    start_seconds=1.0,
                    end_seconds=3.0,
                    instructions="replace the phrase",
                    lyrics="",
                    revision_number=1,
                    seed=7,
                    crossfade_seconds=0.5,
                )

    def test_zip_requires_exact_safe_members(self) -> None:
        extracted = extract_expected_zip(
            zip_bytes({"instrumental.wav": b"instrumental", "vocal.wav": b"vocal"}),
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
                body = request.content.decode("latin1")
                for expected in ("rmvpe", "0.3", "3", "0.75", "0.2"):
                    self.assertIn(expected, body)
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
                index_rate=0.3,
                filter_radius=3,
                rms_mix_rate=0.75,
                protect=0.2,
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

    def test_stems_preserve_wet_vocal_and_reverb_when_available(self) -> None:
        files = {
            "instrumental.wav": b"instrumental",
            "vocal_dry_original.wav": b"dry",
            "vocal_wet_original.wav": b"wet",
            "vocal_reverb_original.wav": b"reverb",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/audio/stem-separations")
            return httpx.Response(200, content=zip_bytes(files))

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            source = store.create(
                project_id="music-test",
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
            stems = executor.separate_stems(project_id="music-test", audio_asset_id=source.artifact_id)
            self.assertEqual([item.filename for item in stems], list(files))
            for item in stems:
                self.assertEqual(store.read(item.artifact_id, "music-test")[1], files[item.filename])

    def test_stems_accept_optional_unreviewed_backing_outputs(self) -> None:
        files = {
            "instrumental.wav": b"instrumental",
            "vocal_dry_original.wav": b"dry",
            "vocal_wet_original.wav": b"wet",
            "vocal_reverb_original.wav": b"reverb",
            "vocal_lead_reference.wav": b"lead",
            "backing_vocals_unreviewed.wav": b"backing",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=zip_bytes(files))

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            source = store.create(
                project_id="backing-test",
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
            result = executor.separate_stems(project_id="backing-test", audio_asset_id=source.artifact_id)
            self.assertEqual([item.filename for item in result], list(files))

    def test_master_sends_independent_backing_vocal_when_requested(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/audio/masters")
            body = request.content.decode("latin1")
            self.assertIn('name="backing_vocal"', body)
            self.assertIn('name="backing_gain_db"', body)
            self.assertIn("-4.0", body)
            self.assertIn("backing-content", body)
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

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            records = [
                store.create(
                    project_id="music-test",
                    operation="asset.import",
                    filename=name,
                    media_type="audio/wav",
                    payload=content,
                )
                for name, content in (
                    ("instrumental.wav", b"instrumental"),
                    ("vocal_dry_cloned.wav", b"lead"),
                    ("backing_vocals.wav", b"backing-content"),
                )
            ]
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            result = executor.master(
                project_id="music-test",
                instrumental_asset_id=records[0].artifact_id,
                vocal_asset_id=records[1].artifact_id,
                backing_vocal_asset_id=records[2].artifact_id,
                backing_gain_db=-4.0,
                lyrics_lrc="[00:00.00]line",
                bpm=120,
            )
            self.assertEqual(result[0].filename, "Final_Song_Master.wav")

    def test_master_rejects_wet_vocal_as_backing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            records = [
                store.create(
                    project_id="music-test",
                    operation="asset.import",
                    filename=name,
                    media_type="audio/wav",
                    payload=b"audio",
                )
                for name in ("instrumental.wav", "vocal_dry_cloned.wav", "vocal_wet_original.wav")
            ]
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
            )
            with self.assertRaisesRegex(ValueError, "not independent backing vocals"):
                executor.master(
                    project_id="music-test",
                    instrumental_asset_id=records[0].artifact_id,
                    vocal_asset_id=records[1].artifact_id,
                    backing_vocal_asset_id=records[2].artifact_id,
                    lyrics_lrc="[00:00.00]line",
                    bpm=120,
                )

    def test_master_sends_duet_parts_and_rejects_incomplete_choir(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("latin1")
            self.assertIn('name="additional_vocals"', body)
            self.assertIn('name="vocal_mode"', body)
            self.assertIn("second-singer", body)
            if "choir" in body:
                self.assertEqual(body.count('name="additional_vocals"'), 2)
                self.assertEqual(body.count('name="additional_vocal_gains_db"'), 2)
                self.assertIn("third-singer", body)
                self.assertIn("-6.0", body)
            else:
                self.assertIn("duet", body)
                self.assertEqual(body.count('name="additional_vocals"'), 1)
            return httpx.Response(
                200,
                content=zip_bytes({
                    "Final_Song_Master.wav": b"master",
                    "Final_Song.mp3": b"mp3",
                    "Final_Song.lrc": b"[00:00.00]line",
                }),
            )

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            records = [
                store.create(
                    project_id="duet-test",
                    operation="asset.import",
                    filename=name,
                    media_type="audio/wav",
                    payload=payload,
                )
                for name, payload in (
                    ("instrumental.wav", b"instrumental"),
                    ("voice-a.wav", b"first-singer"),
                    ("voice-b.wav", b"second-singer"),
                    ("voice-c.wav", b"third-singer"),
                )
            ]
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            with self.assertRaisesRegex(ValueError, "do not match"):
                executor.master(
                    project_id="duet-test",
                    instrumental_asset_id=records[0].artifact_id,
                    vocal_asset_id=records[1].artifact_id,
                    additional_vocal_asset_ids=[records[2].artifact_id],
                    vocal_mode="choir",
                    lyrics_lrc="[00:00.00]line",
                    bpm=120,
                )
            final = executor.master(
                project_id="duet-test",
                instrumental_asset_id=records[0].artifact_id,
                vocal_asset_id=records[1].artifact_id,
                additional_vocal_asset_ids=[records[2].artifact_id],
                vocal_mode="duet",
                lyrics_lrc="[00:00.00]line",
                bpm=120,
            )
            self.assertEqual(final[0].filename, "Final_Song_Master.wav")
            choir = executor.master(
                project_id="duet-test",
                instrumental_asset_id=records[0].artifact_id,
                vocal_asset_id=records[1].artifact_id,
                additional_vocal_asset_ids=[records[2].artifact_id, records[3].artifact_id],
                additional_vocal_gains_db=[-3.0, -6.0],
                vocal_mode="choir",
                lyrics_lrc="[00:00.00]line",
                bpm=120,
            )
            self.assertEqual(choir[0].filename, "Final_Song_Master.wav")

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

    def test_voice_preparation_posts_ordered_sources_and_persists_review_outputs(self) -> None:
        seen_filenames: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read()
            for filename in ("first.m4a", "second.wav"):
                self.assertIn(filename.encode(), body)
                seen_filenames.append(filename)
            return httpx.Response(200, content=voice_preparation_zip(2))

        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            first = store.create(
                project_id="voice-project",
                operation="asset.import",
                filename="first.m4a",
                media_type="audio/mp4",
                payload=b"first",
            )
            second = store.create(
                project_id="voice-project",
                operation="asset.import",
                filename="second.wav",
                media_type="audio/wav",
                payload=b"second",
            )
            executor = MusicExecutor(
                store,
                ServiceURLs("http://music", "http://uvr", "http://rvc", "http://align", "http://mix"),
                httpx.Client(transport=httpx.MockTransport(handler)),
            )
            outputs = executor.prepare_voice(
                project_id="voice-project",
                audio_asset_ids=[first.artifact_id, second.artifact_id],
            )
            self.assertEqual(seen_filenames, ["first.m4a", "second.wav"])
            self.assertEqual(
                [record.filename for record in outputs],
                [
                    "prepared-01.wav",
                    "prepared-02.wav",
                    "voice-training.wav",
                    "voice-segments.zip",
                    "voice-preparation-report.json",
                ],
            )
            self.assertTrue(all(record.operation == "voice.prepare" for record in outputs))

            with self.assertRaisesRegex(ValueError, "duplicates"):
                executor.prepare_voice(
                    project_id="voice-project",
                    audio_asset_ids=[first.artifact_id, first.artifact_id],
                )
            with self.assertRaises(ArtifactError):
                executor.prepare_voice(
                    project_id="another-project",
                    audio_asset_ids=[first.artifact_id],
                )

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
