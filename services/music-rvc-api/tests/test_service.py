from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from verdantflare_rvc.catalog import VoiceModelCatalog
from verdantflare_rvc.service import ConversionFailed, RVCService


class FakeVC:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.loaded_model = None
        self.conversion_args = None

    def get_vc(self, model_name: str) -> None:
        self.loaded_model = model_name

    def vc_single(self, *args: object) -> tuple[str, tuple[int | None, object | None]]:
        self.conversion_args = args
        if not self.success:
            return "traceback", (None, None)
        return "Success.", (40000, [0.0, 0.1])


class RVCServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "voices"
        model_directory = self.root / "lead-vocal"
        model_directory.mkdir(parents=True)
        (model_directory / "model.pth").write_bytes(b"checkpoint")
        self.catalog = VoiceModelCatalog(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_converts_with_catalog_model_and_disables_missing_index(self) -> None:
        backend = FakeVC()
        service = RVCService(self.catalog)
        service._vc = backend

        sample_rate, audio = service.convert(
            model_id="lead-vocal",
            input_path=Path("input.wav"),
            speaker_id=0,
            pitch_shift=2,
            f0_method="rmvpe",
            index_rate=0.66,
            filter_radius=3,
            resample_sr=0,
            rms_mix_rate=1.0,
            protect=0.33,
        )

        self.assertEqual(backend.loaded_model, "lead-vocal/model.pth")
        self.assertEqual(backend.conversion_args[7], 0.0)
        self.assertEqual(sample_rate, 40000)
        self.assertEqual(audio, [0.0, 0.1])

    def test_raises_stable_error_when_upstream_conversion_fails(self) -> None:
        service = RVCService(self.catalog)
        service._vc = FakeVC(success=False)

        with self.assertRaisesRegex(ConversionFailed, "voice conversion failed"):
            service.convert(
                model_id="lead-vocal",
                input_path=Path("input.wav"),
                speaker_id=0,
                pitch_shift=0,
                f0_method="rmvpe",
                index_rate=0.0,
                filter_radius=3,
                resample_sr=0,
                rms_mix_rate=1.0,
                protect=0.33,
            )


if __name__ == "__main__":
    unittest.main()
