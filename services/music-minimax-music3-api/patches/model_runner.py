# SPDX-License-Identifier: Apache-2.0
"""Opt-in trajectory capture and isolated prefix experiment over pinned Omni.

Default generation is unchanged. CPU tests are not music-quality validation.
"""
from __future__ import annotations

import os
from pathlib import Path

import torch

from .model_runner_upstream import MiniMaxMusic3ModelRunner as UpstreamRunner
from .trajectory import Prefix, Trajectory


class MiniMaxMusic3ModelRunner(UpstreamRunner):
    def __init__(self, tp_worker, output_processor):
        super().__init__(tp_worker, output_processor)
        self._trajectory_root = os.environ.get('VF_MUSIC3_TRAJECTORY_DIR')
        prefix_config = os.environ.get('VF_MUSIC3_PREFIX_EXPERIMENT')
        if prefix_config and not self._trajectory_root:
            raise ValueError('Prefix experiment requires trajectory capture')
        if (prefix_config or self._trajectory_root) and self._forced_codes_dir:
            raise ValueError('Research capture cannot use upstream reference replay')
        self._prefix = Prefix(Path(prefix_config)) if prefix_config else None
        self._trajectories = {}
        self._replayed_c0 = None

    def _start_request(self, request_id, data, device):
        state = data.minimax_state
        if self._prefix:
            self._prefix.claim(state.prompt, state.seed, state.max_audio_frames)
        super()._start_request(request_id, data, device)
        if self._trajectory_root:
            self._trajectories[request_id] = Trajectory(
                request_id, state.prompt, state.seed,
                provenance=self._prefix.provenance if self._prefix else None,
            )

    def _forced_codes_for(self, ar_states, device):
        if self._prefix is None:
            return super()._forced_codes_for(ar_states, device)
        if len(ar_states) != 1:
            raise ValueError('Prefix experiment cannot mix requests in a batch')
        row = self._prefix.next_codes()
        if row is None:
            return None
        codes = torch.tensor([row], dtype=torch.int64, device=device)
        self._replayed_c0 = codes[:, 0] + 1
        return codes

    def _advance(self, result, requests, *, emit):
        self._replayed_c0 = None
        super()._advance(result, requests, emit=emit)
        if self._replayed_c0 is not None:
            # Upstream reference replay overwrites audio codes but leaves the
            # sampled token (potentially EOS) in the scheduler's return value.
            result.next_token_ids = self.model.c0_logit_ids[
                self._replayed_c0.repeat_interleave(2)
            ]
        if not self._trajectory_root:
            return
        for request in requests[0::2]:
            state = self._ar_state(request)
            if state.finish_reason == 'stop':
                continue  # EOS is not a new audio-code frame.
            codes = state.feedback_codes[0].detach().cpu().tolist()
            self._trajectories[request.request_id].append(codes, state.sampling_position)

    def on_request_finished(self, request_id, req_data):
        super().on_request_finished(request_id, req_data)
        trajectory = self._trajectories.pop(request_id, None)
        if trajectory is not None:
            state = req_data.ar_state
            trajectory.save(Path(self._trajectory_root), state.generated_frames, state.finish_reason)

    def reset_request(self, request_id):
        self._trajectories.pop(request_id, None)
        super().reset_request(request_id)
