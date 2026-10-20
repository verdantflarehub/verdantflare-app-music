"""Exercise the actual pinned upstream _advance on CPU with fake model heads.

Set MUSIC3_UPSTREAM_SOURCE to the extracted, verified sgLang-Omni 0.1.3 module
folder. This test does not load model weights or judge generated music.
"""
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import torch

import test_trajectory as trajectory_tests
from test_trajectory import t, PATCHES


class RunnerTest(unittest.TestCase):
    fixture = trajectory_tests.TrajectoryTest.fixture
    def setUp(self):
        trajectory_tests.TrajectoryTest.setUp(self)
        source = Path(os.environ['MUSIC3_UPSTREAM_SOURCE'])
        if t.digest((source / 'model_runner.py').read_bytes()) != t.RUNNER_SHA256:
            raise AssertionError('Must test the actual pinned upstream runner')
        modules = {}
        for name in ['sglang_omni', 'sglang_omni.model_runner', 'sglang_omni.model_runner.base',
                     'sglang_omni.sampling', 'sglang_omni.sampling.seed', 'music3_test']:
            modules[name] = types.ModuleType(name)
        class Base:
            def __init__(self, tp_worker, processor):
                self.model = tp_worker
        modules['sglang_omni.model_runner.base'].ModelRunner = Base
        modules['sglang_omni.sampling.seed'].derive_sampling_seed = lambda namespace, seed: seed
        modules['music3_test'].__path__ = [str(source)]
        model = types.ModuleType('music3_test.sglang_model')
        model.apply_cfg = lambda cond, uncond: cond
        model.embed_audio_frames = lambda *args: None
        model.select_c0_logits = lambda model, logits: logits
        def depth(model, hidden, c0, seeds, positions, forced):
            codes = forced if forced is not None else c0[:, None].repeat(1, 8)
            return codes, hidden, torch.zeros_like(codes)
        model.depth_decode = depth
        modules[model.__name__] = model
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start(); self.addCleanup(self.module_patch.stop)
        self.env_patch = patch.dict(os.environ, {}, clear=False)
        self.env_patch.start(); self.addCleanup(self.env_patch.stop)
        for key in ['VF_MUSIC3_TRAJECTORY_DIR', 'VF_MUSIC3_PREFIX_EXPERIMENT',
                    'MINIMAX_MUSIC3_FORCED_CODES', 'MINIMAX_MUSIC3_HIDDEN_DUMP']:
            os.environ.pop(key, None)
        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        self.upstream = load('music3_test.model_runner_upstream', source / 'model_runner.py')
        sys.modules['music3_test.trajectory'] = t
        self.wrapper = load('music3_test.model_runner', PATCHES / 'model_runner.py')
        self.model = types.SimpleNamespace(num_codebooks=8, c0_logit_ids=torch.arange(16385) + 100)

    def runner(self, prefix=False, capture=True):
        if capture:
            os.environ['VF_MUSIC3_TRAJECTORY_DIR'] = str(self.root / 'out')
        if prefix:
            config, _, _ = self.fixture()
            os.environ['VF_MUSIC3_PREFIX_EXPERIMENT'] = str(config)
        runner = self.wrapper.MiniMaxMusic3ModelRunner(self.model, None)
        runner._emit_ready_window = lambda *a: None
        runner._emit_window = lambda *a, **kw: None
        state = types.SimpleNamespace(prompt='full prompt', seed=613, max_audio_frames=100)
        data = types.SimpleNamespace(minimax_state=state, prompt_tokens=12, ar_state=None)
        uncond = types.SimpleNamespace(request_id='r-cfg', data=types.SimpleNamespace())
        data.cfg_uncond = uncond.data
        cond = types.SimpleNamespace(request_id='r', data=data)
        runner._start_request('r', data, 'cpu')
        return runner, [cond, uncond]

    def advance(self, runner, requests, sampled, emit):
        result = types.SimpleNamespace(logits_output=types.SimpleNamespace(
            hidden_states=torch.ones(2, 4), next_token_logits=torch.zeros(2, 16385)))
        with patch.object(self.upstream, 'sample_topk_seeded', return_value=torch.tensor([sampled])):
            runner._advance(result, requests, emit=emit)
        return result

    def test_prefix_eos_is_masked_then_sampling_and_eos_resume(self):
        runner, requests = self.runner(prefix=True)
        result = self.advance(runner, requests, 0, False)
        self.assertEqual(result.next_token_ids.tolist(), [101, 101])
        self.assertEqual(requests[0].data.ar_state.feedback_codes.tolist(), [[0] * 8])
        result = self.advance(runner, requests, 0, True)
        self.assertEqual(result.next_token_ids.tolist(), [102, 102])
        result = self.advance(runner, requests, 4, True)
        self.assertEqual(result.next_token_ids.tolist(), [104, 104])
        self.assertEqual(requests[0].data.ar_state.feedback_codes.tolist(), [[3] * 8])
        self.assertEqual(len(runner._trajectories['r'].codes), 3)
        result = self.advance(runner, requests, 0, True)
        self.assertEqual(result.next_token_ids.tolist(), [100, 100])
        self.assertEqual(len(runner._trajectories['r'].codes), 3)
        runner.on_request_finished('r', requests[0].data)
        saved, _ = t.read_json(next((self.root / 'out').glob('*.json')))
        self.assertEqual(saved['codes'], [[0] * 8, [1] * 8, [3] * 8])
        self.assertEqual(saved['generated_frames'], 2)
        self.assertEqual(saved['finish_reason'], 'stop')

    def test_disabled_capture_does_not_change_sampled_tokens(self):
        runner, requests = self.runner(capture=False)
        result = self.advance(runner, requests, 3, False)
        self.assertEqual(result.next_token_ids.tolist(), [103, 103])
        self.assertEqual(runner._trajectories, {})
        self.assertFalse((self.root / 'out').exists())

    def test_aborted_capture_does_not_publish_completed_state(self):
        runner, requests = self.runner()
        self.advance(runner, requests, 2, False)
        runner.reset_request('r')
        self.assertEqual(runner._trajectories, {})
        self.assertFalse((self.root / 'out').exists())

    def test_mixed_prefix_batch_is_rejected(self):
        runner, requests = self.runner(prefix=True)
        with self.assertRaisesRegex(ValueError, 'mix'):
            runner._forced_codes_for([requests[0].data.ar_state] * 2, 'cpu')

    def test_reference_replay_cannot_masquerade_as_capture(self):
        os.environ['VF_MUSIC3_TRAJECTORY_DIR'] = str(self.root)
        os.environ['MINIMAX_MUSIC3_FORCED_CODES'] = str(self.root)
        with self.assertRaisesRegex(ValueError, 'reference replay'):
            self.wrapper.MiniMaxMusic3ModelRunner(self.model, None)


if __name__ == '__main__':
    unittest.main()
