import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import wave

PATCHES = Path(__file__).parents[1] / 'patches'
spec = importlib.util.spec_from_file_location('trajectory', PATCHES / 'trajectory.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


class TrajectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self):
        for name in ['original.wav', 'recovered.wav']:
            with wave.open(str(self.root / name), 'wb') as wav:
                wav.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
                wav.writeframes(b'\x01\x00' * 64)
        capture = t.Trajectory('request-a', 'original prompt', 613)
        for step in range(3):
            capture.append([step] * 8, (step + 1) * 8)
        path = capture.save(self.root, 2, 'stop')
        state, state_hash = t.read_json(path)
        receipt = self.root / 'receipt.json'
        t.atomic_create(receipt, {'trajectory_sha256': state_hash,
                                 'request_sha256': state['request_sha256'],
                                 'prompt_sha256': state['prompt_sha256'],
                                 'pcm_sha256': t.pcm_identity(self.root / 'recovered.wav')})
        config = {'trajectory_path': str(path), 'trajectory_sha256': state_hash,
                  'original_wav_path': str(self.root / 'original.wav'),
                  'recovered_wav_path': str(self.root / 'recovered.wav'),
                  'recovery_receipt_path': str(receipt), 'prefix_steps': 2,
                  'target_prompt_sha256': t.prompt_digest('full prompt'), 'target_seed': 613}
        config_path = self.root / 'prefix.json'
        t.atomic_create(config_path, config)
        return config_path, config, path

    def test_source_is_bound_to_original_pcm_and_request(self):
        config_path, _, _ = self.fixture()
        prefix = t.Prefix(config_path)
        prefix.claim('full prompt', 613, 100)
        self.assertEqual(prefix.next_codes(), [0] * 8)
        self.assertEqual(prefix.next_codes(), [1] * 8)
        self.assertIsNone(prefix.next_codes())
        self.assertIsNone(prefix.next_codes())
        with self.assertRaises(ValueError):
            prefix.claim('full prompt', 613, 100)

    def test_same_seed_changed_audio_is_rejected(self):
        config_path, _, _ = self.fixture()
        with wave.open(str(self.root / 'recovered.wav'), 'wb') as wav:
            wav.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
            wav.writeframes(b'\x02\x00' * 64)
        with self.assertRaisesRegex(ValueError, 'original PCM'):
            t.Prefix(config_path)

    def test_unrelated_receipt_is_rejected(self):
        config_path, config, _ = self.fixture()
        receipt = Path(config['recovery_receipt_path'])
        doc = json.loads(receipt.read_text()); doc['request_sha256'] = '0' * 64
        receipt.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'bind'):
            t.Prefix(config_path)

    def test_tampered_or_incompatible_trajectory_is_rejected(self):
        config_path, config, path = self.fixture()
        doc = json.loads(path.read_text()); doc['model_revision'] = 'wrong'
        path.write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'digest'):
            t.Prefix(config_path)
        config['trajectory_sha256'] = t.digest(path.read_bytes())
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            t.Prefix(config_path)

    def test_target_prompt_seed_and_free_generation_budget_are_checked(self):
        config_path, _, _ = self.fixture()
        for prompt, seed, frames in [('changed', 613, 100), ('full prompt', 9, 100), ('full prompt', 613, 1)]:
            with self.subTest(prompt=prompt, seed=seed, frames=frames), self.assertRaises(ValueError):
                t.Prefix(config_path).claim(prompt, seed, frames)

    def test_bootstrap_and_sampling_position_cannot_be_omitted(self):
        capture = t.Trajectory('r', 'prompt', 1)
        with self.assertRaisesRegex(ValueError, 'sampling'):
            capture.append([0] * 8, 16)
        capture.append([0] * 8, 8)
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            capture.save(self.root, 1, 'length')

    def test_request_isolation_no_overwrite_and_no_cleartext_prompt(self):
        for request in ['a', 'b']:
            capture = t.Trajectory(request, 'private lyrics', 613)
            capture.append([0] * 8, 8)
            path = capture.save(self.root, 0, 'stop')
            self.assertNotIn('private lyrics', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                capture.save(self.root, 0, 'stop')
        self.assertEqual(len(list(self.root.glob('*.json'))), 2)
        self.assertFalse(list(self.root.glob('.trajectory-*')))

    def test_code_shape_range_and_types(self):
        for row in [[0] * 7, [16384] + [0] * 7, [0, 1024] + [0] * 6, [True] * 8]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                t.validate_codes([row])

    def test_state_file_size_is_bounded(self):
        path = self.root / 'oversized.json'
        path.write_bytes(b' ' * (t.MAX_JSON_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'size'):
            t.read_json(path)


if __name__ == '__main__':
    unittest.main()
