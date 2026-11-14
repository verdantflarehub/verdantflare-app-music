from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class InvalidModelId(ValueError):
    pass


class VoiceModelNotFound(LookupError):
    pass


@dataclass(frozen=True)
class VoiceModel:
    model_id: str
    checkpoint_path: Path
    index_path: Path | None

    @property
    def upstream_name(self) -> str:
        return f"{self.model_id}/model.pth"


class VoiceModelCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def validate_model_id(model_id: str) -> str:
        if not MODEL_ID_PATTERN.fullmatch(model_id):
            raise InvalidModelId(
                "model_id must contain 1-64 ASCII letters, digits, dots, underscores, or hyphens"
            )
        return model_id

    def list_models(self) -> list[VoiceModel]:
        if not self.root.is_dir():
            return []

        models: list[VoiceModel] = []
        for directory in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not directory.is_dir() or not MODEL_ID_PATTERN.fullmatch(directory.name):
                continue
            try:
                models.append(self.resolve(directory.name))
            except VoiceModelNotFound:
                continue
        return models

    def resolve(self, model_id: str) -> VoiceModel:
        self.validate_model_id(model_id)
        root = self.root.resolve()
        directory = (root / model_id).resolve()
        if directory.parent != root or not directory.is_dir():
            raise VoiceModelNotFound(f"voice model '{model_id}' was not found")

        checkpoint = (directory / "model.pth").resolve()
        if checkpoint.parent != directory or not checkpoint.is_file():
            raise VoiceModelNotFound(f"voice model '{model_id}' was not found")

        index = (directory / "model.index").resolve()
        if index.parent != directory or not index.is_file():
            index = None

        return VoiceModel(
            model_id=model_id,
            checkpoint_path=checkpoint,
            index_path=index,
        )
