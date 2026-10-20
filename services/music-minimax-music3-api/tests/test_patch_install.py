import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

PATCHES = Path(__file__).parents[1] / 'patches'
spec = importlib.util.spec_from_file_location('patch_install', PATCHES / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTest(unittest.TestCase):
    def test_unknown_upstream_fails_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'model_runner.py'
            original.write_text('unknown source')
            with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
                installer.install(root, PATCHES)
            self.assertEqual(original.read_text(), 'unknown source')
            self.assertFalse((root / 'model_runner_upstream.py').exists())

    def test_install_preserves_pinned_upstream_and_refuses_second_install(self):
        source = Path(os.environ['MUSIC3_UPSTREAM_SOURCE']) / 'model_runner.py'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model_runner.py').write_bytes(source.read_bytes())
            installer.install(root, PATCHES)
            self.assertEqual((root / 'model_runner_upstream.py').read_bytes(), source.read_bytes())
            self.assertEqual((root / 'model_runner.py').read_bytes(), (PATCHES / 'model_runner.py').read_bytes())
            with self.assertRaises(RuntimeError):
                installer.install(root, PATCHES)
