import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import wave
from unittest.mock import patch

import httpx

from verdantflare_music_mcp.artifacts import ArtifactStore, ArtifactNotFound
from verdantflare_music_mcp.continuation import append_generated, source_pcm, validate
from verdantflare_music_mcp.executor import ExecutionError, MusicExecutor, ServiceURLs
from verdantflare_music_mcp.production import ProductionJobs
from test_executor import wav_bytes, extensible_wav


class ContinuationTest(unittest.TestCase):
    def test_exact_24bit_prefix_and_generated_tail(self):
        src = io.BytesIO()
        frame = (1234567).to_bytes(3, 'little', signed=True) + (-2345678).to_bytes(3, 'little', signed=True)
        with wave.open(src, 'wb') as w:
            w.setparams((2, 3, 32000, 0, 'NONE', 'not compressed'))
            w.writeframes(frame * 32000)
        source = extensible_wav(src.getvalue())
        output, report = append_generated(source, wav_bytes(5, 48000, 800), 3)
        p, result = source_pcm(output)
        self.assertEqual((p.sampwidth, p.nchannels, p.framerate, p.nframes), (3, 2, 32000, 128000))
        self.assertEqual(result[:32000 * 6], frame * 32000)
        self.assertNotEqual(result[32000 * 6:], bytes(96000 * 6))
        self.assertEqual(report['preserved_prefix_pcm_sha256'], hashlib.sha256(frame * 32000).hexdigest())
        self.assertEqual(report['musical_review'], 'unreviewed')

    def test_short_or_silent_result_never_padded(self):
        src = wav_bytes(1)
        for output, message in [(wav_bytes(2), 'shorter'), (wav_bytes(4, sample_value=0), 'silence')]:
            with self.subTest(message=message), self.assertRaisesRegex(ExecutionError, message):
                append_generated(src, output, 3)

    def test_invalid_duration_total_and_project_rejected_before_submission(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d))
            ex = MusicExecutor(store, ServiceURLs(*(['http://backend'] * 6)))
            a = store.create(project_id='p', operation='test', filename='source.wav', media_type='audio/wav', payload=wav_bytes(298, 8000))
            cfg = dict(addition_seconds=3, instructions='continue', lyrics='', seed=1)
            with self.assertRaisesRegex(ValueError, '300'):
                validate(ex, 'p', a.artifact_id, cfg)
            for value in (0, -1, 61, float('inf'), float('nan'), True):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    validate(ex, 'p', a.artifact_id, dict(cfg, addition_seconds=value))
            with self.assertRaises(ArtifactNotFound):
                validate(ex, 'other', a.artifact_id, cfg)

    def test_legacy_job_recovery_and_idempotent_receipt(self):
        calls = []
        original = wav_bytes(1, 48000, 1200)
        def handler(req):
            calls.append(req.url.path)
            if req.url.path == '/health':
                return httpx.Response(200, json={'status': 'ok'})
            if req.url.path == '/release_task':
                self.assertIn(original, req.content)  # unpadded source, uploaded verbatim
                for field in (b'repainting_start', b'1.0', b'repainting_end', b'4.0', b'explicit', b'48'):
                    self.assertIn(field, req.content)
                return httpx.Response(200, json={'code': 200, 'data': {'task_id': 'native-task'}})
            if req.url.path == '/query_result':
                self.assertEqual(json.loads(req.content)['task_id_list'], ['native-task'])
                return httpx.Response(200, json={'code': 200, 'data': [{'task_id': 'native-task', 'status': 1, 'result': [{'file': '/output.wav'}]}]})
            if req.url.path == '/output.wav':
                return httpx.Response(200, content=wav_bytes(4, 48000, 300))
            raise AssertionError(req.url.path)
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d)); store.ensure_ready()
            ex = MusicExecutor(store, ServiceURLs(*(['http://backend'] * 6)), httpx.Client(transport=httpx.MockTransport(handler)))
            a = store.create(project_id='p', operation='test', filename='source.wav', media_type='audio/wav', payload=original)
            jobs = ProductionJobs(ex, lambda *a: None)
            rid = str(uuid.uuid4()); cfg = dict(addition_seconds=3, instructions='continue', lyrics='new verse', seed=12)
            payload = dict(operation='music.extend', config=cfg, asset_ids=[a.artifact_id], inputs_sha256=[a.sha256])
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
            jobs._save(jobs._path('p', rid), dict(payload, project_id='p', request_id=rid,
                fingerprint=fingerprint, epoch='previous-worker', state='failed', artifacts=[], backend_task_id='native-task'))
            jobs.resume_extension('p', rid); jobs.pool.shutdown(wait=True)
            result = jobs.status('p', rid)
            self.assertEqual(result['state'], 'completed', result)
            self.assertEqual(result['backend_task_id'], 'native-task')
            self.assertEqual(len(result['artifacts']), 2)
            output = next(r for r in result['artifacts'] if r['filename'] == 'render.wav')
            _, raw = store.read(output['artifact_id'], 'p')
            self.assertEqual(source_pcm(raw)[1][:len(source_pcm(original)[1])], source_pcm(original)[1])
            self.assertEqual(jobs.submit('p', rid, 'music.extend', cfg, [a.artifact_id]), result)
            self.assertEqual(calls.count('/release_task'), 0)
            with self.assertRaises(ValueError):
                jobs.submit('p', rid, 'music.extend', dict(cfg, seed=13), [a.artifact_id])

    def test_untrusted_result_origin_is_never_downloaded(self):
        calls = []
        def handler(req):
            calls.append(str(req.url))
            if req.url.path == '/release_task':
                return httpx.Response(200, json={'code': 200, 'data': {'task_id': 'x'}})
            return httpx.Response(200, json={'code': 200, 'data': [{'task_id': 'x', 'status': 1, 'result': [{'file': 'https://untrusted.example/audio.wav'}]}]})
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d))
            src = store.create(project_id='p', operation='test', filename='a.wav', media_type='audio/wav', payload=wav_bytes(1))
            ex = MusicExecutor(store, ServiceURLs(*(['http://backend'] * 6)), httpx.Client(transport=httpx.MockTransport(handler)))
            with self.assertRaisesRegex(ExecutionError, 'untrusted origin'):
                ex._conditioned_repaint(src, wav_bytes(1), 1, 4, 'continue', '', 1, .05, 'aggressive', 1)
            self.assertEqual(len(calls), 2)

    def test_interrupted_receipt_keeps_backend_identity(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d)); store.ensure_ready()
            jobs = ProductionJobs(MusicExecutor(store, ServiceURLs(*(['http://backend'] * 5))), lambda *a: None)
            rid = str(uuid.uuid4())
            jobs._save(jobs._path('p', rid), {'epoch': 'old-worker', 'state': 'running', 'artifacts': [], 'backend_task_id': 'recover-me'})
            status = jobs.status('p', rid)
            self.assertEqual(status['state'], 'interrupted')
            self.assertEqual(status['backend_task_id'], 'recover-me')
            jobs.pool.shutdown()

    def test_cancellation_after_submission_suppresses_publication(self):
        with tempfile.TemporaryDirectory() as d:
            store = ArtifactStore(Path(d)); store.ensure_ready()
            ex = MusicExecutor(store, ServiceURLs(*(['http://backend'] * 6)))
            src = store.create(project_id='p', operation='test', filename='a.wav', media_type='audio/wav', payload=wav_bytes(1))
            jobs = ProductionJobs(ex, lambda *a: self.fail('cancelled output published'))
            rid = str(uuid.uuid4())
            def render(executor, project, asset, config, callback, resume_task_id=None):
                callback('cancel-me'); jobs.status('p', rid, cancel=True)
                raw = wav_bytes(4)
                return {'render.wav': raw, 'report.json': json.dumps({'outputs_sha256': {'render.wav': hashlib.sha256(raw).hexdigest()}}).encode()}
            with patch('verdantflare_music_mcp.production.continuation.render', side_effect=render):
                jobs._save(jobs._path('p', rid), dict(operation='music.extend', project_id='p', request_id=rid,
                    config=dict(addition_seconds=3, instructions='continue', lyrics='', seed=1),
                    asset_ids=[src.artifact_id], epoch='previous-worker', state='failed', artifacts=[], backend_task_id='cancel-me'))
                jobs.resume_extension('p', rid)
                jobs.pool.shutdown(wait=True)
            status = jobs.status('p', rid)
            self.assertEqual(status['state'], 'cancelled'); self.assertEqual(status['artifacts'], [])
            self.assertEqual(status['backend_task_id'], 'cancel-me')

    def test_latent_rounding_keeps_actual_end_without_padding(self):
        src = wav_bytes(1, 48000)
        output, report = append_generated(src, wav_bytes(3.984, 48000), 3)
        self.assertEqual(source_pcm(output)[0].nframes, round(3.984 * 48000))
        self.assertAlmostEqual(report['duration_shortfall_seconds'], .016)
        with self.assertRaisesRegex(ExecutionError, 'shorter'):
            append_generated(src, wav_bytes(3.95, 48000), 3)

    def test_resume_uses_original_backend_task_without_submission(self):
        calls = []
        def handler(req):
            calls.append(req.url.path)
            if req.url.path == '/query_result':
                self.assertEqual(json.loads(req.content)['task_id_list'], ['prior-task'])
                return httpx.Response(200, json={'code': 200, 'data': [{'task_id': 'prior-task', 'status': 1, 'result': [{'file': '/output.wav'}]}]})
            if req.url.path == '/output.wav':
                return httpx.Response(200, content=wav_bytes(4))
            self.fail('recovery attempted new inference: ' + req.url.path)
        with tempfile.TemporaryDirectory() as d:
            ex = MusicExecutor(ArtifactStore(Path(d)), ServiceURLs(*(['http://backend'] * 6)), httpx.Client(transport=httpx.MockTransport(handler)))
            raw, identity = ex._conditioned_repaint(None, b'', 1, 4, 'continue', '', 1, .05, 'aggressive', 1, resume_task_id='prior-task')
            self.assertEqual(identity, 'prior-task')
            self.assertEqual(calls, ['/query_result', '/output.wav'])
