from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from verdantflare_rvc.catalog import VoiceModelCatalog
from verdantflare_rvc.training import RVCTrainer, TrainingFailed


class RVCTrainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.catalog = VoiceModelCatalog(self.root / "voices")
        self.trainer = RVCTrainer(
            catalog=self.catalog,
            upstream_root=self.root / "upstream",
            runtime_root=self.root / "runtime",
            work_root=self.root / "work",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_rejects_existing_immutable_model(self) -> None:
        model = self.catalog.root / "TonyStark"
        model.mkdir(parents=True)
        (model / "model.pth").write_bytes(b"checkpoint")
        source = self.root / "voice.wav"
        source.write_bytes(b"audio")

        with self.assertRaisesRegex(TrainingFailed, "already exists"):
            self.trainer.train(
                model_id="TonyStark",
                source_audio=source,
                epochs=10,
                batch_size=1,
                save_every_epochs=5,
            )

    def test_writes_training_filelist(self) -> None:
        experiment = self.root / "experiment"
        for directory in ("0_gt_wavs", "3_feature768", "2a_f0", "2b-f0nsf"):
            (experiment / directory).mkdir(parents=True)
        (experiment / "0_gt_wavs" / "slice.wav").touch()
        (experiment / "3_feature768" / "slice.npy").touch()
        (experiment / "2a_f0" / "slice.wav.npy").touch()
        (experiment / "2b-f0nsf" / "slice.wav.npy").touch()
        config = self.trainer.upstream_root / "configs" / "v1" / "40k.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n")

        self.trainer._write_training_files(experiment)

        lines = (experiment / "filelist.txt").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("slice.wav", lines[0])
        self.assertEqual((experiment / "config.json").read_text(), "{}\n")

    @patch("verdantflare_rvc.training.subprocess.run")
    def test_accepts_upstream_training_exit_code_149(self, run: object) -> None:
        run.return_value.returncode = 149
        log = self.root / "training.log"

        self.trainer._run(["python", "train.py"], log, success_codes=(0, 149))

    def test_prepares_supported_wav_dataset_input(self) -> None:
        source = self.root / "training.audio"
        destination = self.root / "dataset" / "voice.wav"
        log = self.root / "training.log"

        with patch.object(self.trainer, "_run") as run:
            self.trainer._prepare_dataset_audio(source, destination, log)

        run.assert_called_once_with(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "40000",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            log,
        )

    def test_archive_uses_business_output_names(self) -> None:
        checkpoint = self.root / "model.pth"
        index = self.root / "model.index"
        validation = self.root / "validation.wav"
        archive = self.root / "TonyStark.zip"
        checkpoint.write_bytes(b"checkpoint")
        index.write_bytes(b"index")
        validation.write_bytes(b"wave")

        self.trainer._write_archive(
            archive_path=archive,
            model_id="TonyStark",
            checkpoint_path=checkpoint,
            index_path=index,
            epochs=200,
        )
        self.trainer.add_validation_audio(archive, validation, "TonyStark")

        with zipfile.ZipFile(archive) as result:
            self.assertEqual(
                set(result.namelist()),
                {
                    "TonyStark.pth",
                    "TonyStark.index",
                    "TonyStark_validation.wav",
                    "manifest.json",
                },
            )
            manifest = json.loads(result.read("manifest.json"))
            self.assertEqual(manifest["model_id"], "TonyStark")


if __name__ == "__main__":
    unittest.main()
