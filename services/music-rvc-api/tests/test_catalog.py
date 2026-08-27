from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from verdantflare_rvc.catalog import (
    InvalidModelId,
    VoiceModelCatalog,
    VoiceModelNotFound,
)


class VoiceModelCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "voices"
        self.root.mkdir()
        self.catalog = VoiceModelCatalog(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_model(self, model_id: str, *, with_index: bool = False) -> Path:
        directory = self.root / model_id
        directory.mkdir()
        (directory / "model.pth").write_bytes(b"checkpoint")
        if with_index:
            (directory / "model.index").write_bytes(b"index")
        return directory

    def test_lists_complete_models(self) -> None:
        self.create_model("lead-vocal", with_index=True)
        (self.root / "incomplete").mkdir()

        models = self.catalog.list_models()

        self.assertEqual([model.model_id for model in models], ["lead-vocal"])
        self.assertIsNotNone(models[0].index_path)

    def test_rejects_invalid_model_id(self) -> None:
        with self.assertRaises(InvalidModelId):
            self.catalog.resolve("../outside")

    def test_rejects_checkpoint_symlink_outside_model_directory(self) -> None:
        outside = Path(self.temporary_directory.name) / "outside.pth"
        outside.write_bytes(b"checkpoint")
        directory = self.root / "unsafe"
        directory.mkdir()
        (directory / "model.pth").symlink_to(outside)

        with self.assertRaises(VoiceModelNotFound):
            self.catalog.resolve("unsafe")

    def test_reports_missing_model(self) -> None:
        with self.assertRaises(VoiceModelNotFound):
            self.catalog.resolve("missing")


if __name__ == "__main__":
    unittest.main()
