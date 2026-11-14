from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .catalog import VoiceModelCatalog, VoiceModelNotFound
from .locks import GPU_LOCK


class TrainingFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class TrainingResult:
    model_id: str
    checkpoint_path: Path
    index_path: Path
    archive_path: Path


class RVCTrainer:
    def __init__(
        self,
        *,
        catalog: VoiceModelCatalog,
        upstream_root: Path = Path("/workspace/rvc"),
        runtime_root: Path = Path("/models/rvc/runtime"),
        work_root: Path = Path("/tmp/rvc-training"),
    ) -> None:
        self.catalog = catalog
        self.upstream_root = upstream_root
        self.runtime_root = runtime_root
        self.work_root = work_root

    def train(
        self,
        *,
        model_id: str,
        source_audio: Path,
        epochs: int,
        batch_size: int,
        save_every_epochs: int,
    ) -> TrainingResult:
        self.catalog.validate_model_id(model_id)
        self.work_root.mkdir(parents=True, exist_ok=True)
        with GPU_LOCK, tempfile.TemporaryDirectory(
            prefix=f"{model_id}-", dir=self.work_root
        ) as temporary_directory:
            try:
                self.catalog.resolve(model_id)
            except VoiceModelNotFound:
                pass
            else:
                raise TrainingFailed(f"voice model '{model_id}' already exists")

            work_dir = Path(temporary_directory)
            dataset_dir = work_dir / "dataset"
            dataset_dir.mkdir()
            dataset_audio = dataset_dir / "voice.wav"

            experiment_id = f"verdantflare-{model_id}-{os.getpid()}"
            experiment_dir = self.upstream_root / "logs" / experiment_id
            checkpoint_source = self.upstream_root / "assets" / "weights" / f"{experiment_id}.pth"
            log_path = work_dir / "training.log"
            staging_directory = self.catalog.root / f".{model_id}.{os.getpid()}.staging"
            installed = False

            try:
                self._prepare_dataset_audio(source_audio, dataset_audio, log_path)
                self._preprocess(dataset_dir, experiment_dir, log_path)
                self._extract_features(experiment_dir, log_path)
                self._write_training_files(experiment_dir)
                self._run_training(
                    experiment_id=experiment_id,
                    epochs=epochs,
                    batch_size=batch_size,
                    save_every_epochs=save_every_epochs,
                    log_path=log_path,
                )
                index_source = self._build_index(experiment_dir, model_id)
                if not checkpoint_source.is_file():
                    raise TrainingFailed("RVC training did not produce a checkpoint")

                model_directory = self.catalog.root / model_id
                staging_directory.mkdir(parents=True)
                shutil.copyfile(checkpoint_source, staging_directory / "model.pth")
                shutil.copyfile(index_source, staging_directory / "model.index")
                model_directory.parent.mkdir(parents=True, exist_ok=True)
                staging_directory.rename(model_directory)
                installed = True

                archive_path = work_dir / f"{model_id}.zip"
                self._write_archive(
                    archive_path=archive_path,
                    model_id=model_id,
                    checkpoint_path=model_directory / "model.pth",
                    index_path=model_directory / "model.index",
                    epochs=epochs,
                )
                persistent_archive = self.work_root / f"{model_id}-{os.getpid()}.zip"
                shutil.copyfile(archive_path, persistent_archive)
                return TrainingResult(
                    model_id=model_id,
                    checkpoint_path=model_directory / "model.pth",
                    index_path=model_directory / "model.index",
                    archive_path=persistent_archive,
                )
            except Exception:
                if installed:
                    shutil.rmtree(model_directory, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(experiment_dir, ignore_errors=True)
                shutil.rmtree(staging_directory, ignore_errors=True)
                checkpoint_source.unlink(missing_ok=True)

    def _prepare_dataset_audio(
        self, source_audio: Path, dataset_audio: Path, log_path: Path
    ) -> None:
        self._run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source_audio),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "40000",
                "-c:a",
                "pcm_s16le",
                str(dataset_audio),
            ],
            log_path,
        )

    def _run(self, arguments: list[str], log_path: Path, *, success_codes: tuple[int, ...] = (0,)) -> None:
        with log_path.open("ab") as log:
            result = subprocess.run(
                arguments,
                cwd=self.upstream_root,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if result.returncode not in success_codes:
            raise TrainingFailed(f"RVC training stage failed with exit code {result.returncode}")

    def _preprocess(self, dataset_dir: Path, experiment_dir: Path, log_path: Path) -> None:
        experiment_dir.mkdir(parents=True)
        self._run(
            [
                sys.executable,
                "infer/modules/train/preprocess.py",
                str(dataset_dir),
                "40000",
                str(max(1, min(8, os.cpu_count() or 1))),
                str(experiment_dir),
                "False",
                "3.0",
            ],
            log_path,
        )

    def _extract_features(self, experiment_dir: Path, log_path: Path) -> None:
        self._run(
            [
                sys.executable,
                "infer/modules/train/extract/extract_f0_rmvpe.py",
                "1",
                "0",
                "0",
                str(experiment_dir),
                "True",
            ],
            log_path,
        )
        self._run(
            [
                sys.executable,
                "infer/modules/train/extract_feature_print.py",
                "cuda:0",
                "1",
                "0",
                "0",
                str(experiment_dir),
                "v2",
            ],
            log_path,
        )

    def _write_training_files(self, experiment_dir: Path) -> None:
        directories = {
            "wav": experiment_dir / "0_gt_wavs",
            "feature": experiment_dir / "3_feature768",
            "f0": experiment_dir / "2a_f0",
            "f0nsf": experiment_dir / "2b-f0nsf",
        }
        names = set(path.stem for path in directories["wav"].glob("*.wav"))
        names &= {path.name.removesuffix(".npy") for path in directories["feature"].glob("*.npy")}
        names &= {path.name.removesuffix(".wav.npy") for path in directories["f0"].glob("*.wav.npy")}
        names &= {path.name.removesuffix(".wav.npy") for path in directories["f0nsf"].glob("*.wav.npy")}
        if not names:
            raise TrainingFailed("RVC preprocessing produced no usable audio slices")

        lines = [
            "|".join(
                [
                    str(directories["wav"] / f"{name}.wav"),
                    str(directories["feature"] / f"{name}.npy"),
                    str(directories["f0"] / f"{name}.wav.npy"),
                    str(directories["f0nsf"] / f"{name}.wav.npy"),
                    "0",
                ]
            )
            for name in sorted(names)
        ]
        mute_root = self.upstream_root / "logs" / "mute"
        mute_line = "|".join(
            [
                str(mute_root / "0_gt_wavs" / "mute40k.wav"),
                str(mute_root / "3_feature768" / "mute.npy"),
                str(mute_root / "2a_f0" / "mute.wav.npy"),
                str(mute_root / "2b-f0nsf" / "mute.wav.npy"),
                "0",
            ]
        )
        lines.extend([mute_line, mute_line])
        (experiment_dir / "filelist.txt").write_text("\n".join(lines) + "\n")
        shutil.copyfile(
            self.upstream_root / "configs" / "v1" / "40k.json",
            experiment_dir / "config.json",
        )

    def _run_training(
        self,
        *,
        experiment_id: str,
        epochs: int,
        batch_size: int,
        save_every_epochs: int,
        log_path: Path,
    ) -> None:
        self._run(
            [
                sys.executable,
                "infer/modules/train/train.py",
                "-e",
                experiment_id,
                "-sr",
                "40k",
                "-f0",
                "1",
                "-bs",
                str(batch_size),
                "-g",
                "0",
                "-te",
                str(epochs),
                "-se",
                str(save_every_epochs),
                "-pg",
                str(self.runtime_root / "pretrained_v2" / "f0G40k.pth"),
                "-pd",
                str(self.runtime_root / "pretrained_v2" / "f0D40k.pth"),
                "-l",
                "1",
                "-c",
                "0",
                "-sw",
                "0",
                "-v",
                "v2",
            ],
            log_path,
            success_codes=(0, 149),
        )

    @staticmethod
    def _build_index(experiment_dir: Path, model_id: str) -> Path:
        import faiss
        import numpy as np

        feature_files = sorted((experiment_dir / "3_feature768").glob("*.npy"))
        if not feature_files:
            raise TrainingFailed("RVC feature extraction produced no feature vectors")
        features = np.concatenate([np.load(path) for path in feature_files], axis=0)
        if features.shape[0] < 39:
            raise TrainingFailed("RVC training produced too few feature vectors")
        generator = np.random.default_rng(0)
        features = features[generator.permutation(features.shape[0])]
        n_ivf = max(1, min(int(16 * np.sqrt(features.shape[0])), features.shape[0] // 39))
        index = faiss.index_factory(768, f"IVF{n_ivf},Flat")
        faiss.extract_index_ivf(index).nprobe = 1
        index.train(features)
        for start in range(0, features.shape[0], 8192):
            index.add(features[start : start + 8192])
        index_path = experiment_dir / f"{model_id}.index"
        faiss.write_index(index, str(index_path))
        return index_path

    @staticmethod
    def _write_archive(
        *,
        archive_path: Path,
        model_id: str,
        checkpoint_path: Path,
        index_path: Path,
        epochs: int,
    ) -> None:
        manifest = {
            "model_id": model_id,
            "version": "v2",
            "sample_rate": 40000,
            "f0_method": "rmvpe",
            "epochs": epochs,
            "files": [f"{model_id}.pth", f"{model_id}.index"],
        }
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(checkpoint_path, f"{model_id}.pth")
            archive.write(index_path, f"{model_id}.index")
            archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")

    @staticmethod
    def add_validation_audio(archive_path: Path, validation_path: Path, model_id: str) -> None:
        temporary_archive = archive_path.with_suffix(".updated.zip")
        with zipfile.ZipFile(archive_path) as source:
            manifest = json.loads(source.read("manifest.json"))
            manifest["files"].append(f"{model_id}_validation.wav")
            with zipfile.ZipFile(
                temporary_archive, "w", compression=zipfile.ZIP_DEFLATED
            ) as destination:
                for item in source.infolist():
                    if item.filename != "manifest.json":
                        destination.writestr(item, source.read(item.filename))
                destination.write(validation_path, f"{model_id}_validation.wav")
                destination.writestr(
                    "manifest.json", json.dumps(manifest, indent=2) + "\n"
                )
        temporary_archive.replace(archive_path)
