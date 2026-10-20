# SPDX-License-Identifier: Apache-2.0
"""Build-time, version-checked installation; never patches a running worker."""
from pathlib import Path
import hashlib
import shutil
import sys


def install(target: Path, patches: Path):
    original = target / 'model_runner.py'
    expected = '8ed9c69e37e73619608c31f11598bda56146167c15ad8cdbc2b2866bbbe4e031'
    if hashlib.sha256(original.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Unexpected SGLang-Omni Music3 runner; re-audit before patching')
    backup = target / 'model_runner_upstream.py'
    if backup.exists():
        raise RuntimeError('Music3 runner already patched')
    original.rename(backup)
    for name in ('model_runner.py', 'trajectory.py'):
        shutil.copyfile(patches / name, target / name)


if __name__ == '__main__':
    install(Path(sys.argv[1]), Path(__file__).parent)
