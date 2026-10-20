# SPDX-License-Identifier: Apache-2.0
"""Opt-in Music3 research state. Not a public continuation contract."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import wave
from pathlib import Path

MODEL_REVISION = 'fbdf52fbaaca799592917417eb05f1899f1255ec'
RUNNER_SHA256 = '8ed9c69e37e73619608c31f11598bda56146167c15ad8cdbc2b2866bbbe4e031'
MAX_STEPS = 9001  # One bootstrap step, then at most 9000 emitted AR frames.
MAX_JSON_BYTES = 4 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prompt_digest(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt:
        raise ValueError('A complete Music3 prompt is required')
    return digest(prompt.encode('utf-8'))


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'Invalid {name}')
    return value


def read_json(path: Path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Research state exceeds size limit')
    return json.loads(raw), digest(raw)


def atomic_create(path: Path, document: dict):
    """Publish a complete private file, never replace an earlier trajectory."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    raw = (json.dumps(document, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Research state exceeds size limit')
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.trajectory-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Fails if target exists, including a symlink.
    finally:
        os.unlink(temporary)


def validate_codes(codes):
    if not isinstance(codes, list) or not 1 <= len(codes) <= MAX_STEPS:
        raise ValueError('Invalid trajectory length')
    for row in codes:
        if not isinstance(row, list) or len(row) != 8:
            raise ValueError('Music3 requires eight codebooks')
        for index, value in enumerate(row):
            integer(value, 'audio code', 0, 16383 if index == 0 else 1023)


class Trajectory:
    def __init__(self, request_id: str, prompt: str, seed: int, *, provenance=None):
        self.request_digest = digest(request_id.encode())
        self.prompt_sha256 = prompt_digest(prompt)
        self.seed = integer(seed, 'seed', 0, 2**64 - 1)
        self.codes = []
        self.provenance = provenance or {"mode": "sampled"}

    def append(self, codes, sampling_position):
        validate_codes([codes])
        if len(self.codes) >= MAX_STEPS or sampling_position != (len(self.codes) + 1) * 8:
            raise ValueError('Discontinuous Music3 sampling trajectory')
        self.codes.append(codes)

    def save(self, root: Path, generated_frames: int, finish_reason: str):
        if len(self.codes) != generated_frames + 1:
            raise ValueError('Missing bootstrap or emitted Music3 frame')
        if finish_reason not in ('stop', 'length'):
            raise ValueError('Cannot persist an incomplete request as completed AR')
        document = {
            'schema_version': 1, 'model_revision': MODEL_REVISION,
            'runner_sha256': RUNNER_SHA256, 'request_sha256': self.request_digest,
            'prompt_sha256': self.prompt_sha256, 'seed': self.seed,
            'stage': 'ar_complete_audio_unverified', 'generated_frames': generated_frames,
            'bootstrap_steps': 1, 'sampling_position_after_codes': len(self.codes) * 8,
            'finish_reason': finish_reason, 'codes': self.codes,
            'provenance': self.provenance,
        }
        path = root / (self.request_digest + '.json')
        atomic_create(path, document)
        return path


def pcm_identity(path: Path):
    """Exact PCM comparison; neither normalization nor approximate similarity."""
    with wave.open(str(path), 'rb') as stream:
        if stream.getcomptype() != 'NONE' or stream.getnframes() <= 0:
            raise ValueError('Expected nonempty PCM WAV')
        fmt = [stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()]
        h = hashlib.sha256(json.dumps(fmt).encode())
        actual_bytes = 0
        while data := stream.readframes(65536):
            h.update(data)
            actual_bytes += len(data)
        if actual_bytes != fmt[0] * fmt[1] * fmt[3]:
            raise ValueError('Truncated PCM WAV')
        return h.hexdigest()


class Prefix:
    """Operator-only experiment. Never accepts paths from an MCP request."""
    def __init__(self, config_path: Path):
        config, _ = read_json(config_path)
        # Configuration and source files are trusted operator-owned mount data.
        source, actual_hash = read_json(Path(config['trajectory_path']))
        if actual_hash != config['trajectory_sha256']:
            raise ValueError('Trajectory digest mismatch')
        if source.get('schema_version') != 1 or source.get('model_revision') != MODEL_REVISION or source.get('runner_sha256') != RUNNER_SHA256:
            raise ValueError('Incompatible Music3 trajectory')
        if source.get('provenance') != {'mode': 'sampled'}:
            raise ValueError('Recovery requires an original sampled trajectory')
        if source.get('stage') != 'ar_complete_audio_unverified' or source.get('bootstrap_steps') != 1:
            raise ValueError('Trajectory is not an AR-complete capture')
        validate_codes(source['codes'])
        if source.get('generated_frames') != len(source['codes']) - 1 or source.get('sampling_position_after_codes') != len(source['codes']) * 8:
            raise ValueError('Trajectory frame count mismatch')
        # Bind capture to the exact recovered request and its output file. The
        # operator creates this receipt only after the acoustic response returns.
        receipt, _ = read_json(Path(config['recovery_receipt_path']))
        if receipt.get('trajectory_sha256') != actual_hash or receipt.get('request_sha256') != source['request_sha256'] or receipt.get('prompt_sha256') != source['prompt_sha256']:
            raise ValueError('Recovery receipt does not bind this trajectory')
        original = pcm_identity(Path(config['original_wav_path']))
        recovered = pcm_identity(Path(config['recovered_wav_path']))
        if original != recovered or recovered != receipt.get('pcm_sha256'):
            raise ValueError('Recovery audio does not exactly match the original PCM')
        count = integer(config['prefix_steps'], 'prefix_steps', 2, len(source['codes']))
        self.provenance = {'mode': 'prefix_experiment', 'source_trajectory_sha256': actual_hash,
                           'source_pcm_sha256': original, 'prefix_steps': count}
        self.codes = source['codes'][:count]
        self.target_prompt = config['target_prompt_sha256']
        if not re.fullmatch('[0-9a-f]{64}', self.target_prompt):
            raise ValueError('Invalid target prompt digest')
        self.target_seed = integer(config['target_seed'], 'target_seed', 0, 2**64 - 1)
        self.index = 0
        self.claimed = False

    def claim(self, prompt: str, seed: int, max_frames: int):
        if self.claimed:
            raise ValueError('Prefix research worker permits one request only')
        if prompt_digest(prompt) != self.target_prompt or seed != self.target_seed:
            raise ValueError('Request does not match the configured prefix experiment')
        if max_frames <= len(self.codes) - 1:
            raise ValueError('No free-generation frames remain after the prefix')
        self.claimed = True

    def next_codes(self):
        if not self.claimed:
            raise ValueError('Prefix has not been bound to a request')
        if self.index == len(self.codes):
            return None  # Free sampling begins; this is not reference replay.
        codes = self.codes[self.index]
        self.index += 1
        return codes
