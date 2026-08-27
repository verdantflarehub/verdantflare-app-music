from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .catalog import VoiceModel, VoiceModelCatalog
from .locks import GPU_LOCK


logger = logging.getLogger(__name__)


def _create_upstream_config(config_type: Any) -> Any:
    process_arguments = sys.argv
    try:
        sys.argv = process_arguments[:1]
        return config_type()
    finally:
        sys.argv = process_arguments


class ConversionFailed(RuntimeError):
    pass


class RVCService:
    def __init__(self, catalog: VoiceModelCatalog) -> None:
        self.catalog = catalog
        self._vc: Any | None = None
        self._loaded_model_id: str | None = None

    def _backend(self) -> Any:
        if self._vc is None:
            from configs.config import Config
            from infer.modules.vc.modules import VC

            self._vc = VC(_create_upstream_config(Config))
        return self._vc

    def _load_model(self, model: VoiceModel) -> Any:
        vc = self._backend()
        if self._loaded_model_id != model.model_id:
            vc.get_vc(model.upstream_name)
            self._loaded_model_id = model.model_id
        return vc

    def convert(
        self,
        *,
        model_id: str,
        input_path: Path,
        speaker_id: int,
        pitch_shift: int,
        f0_method: str,
        index_rate: float,
        filter_radius: int,
        resample_sr: int,
        rms_mix_rate: float,
        protect: float,
    ) -> tuple[int, Any]:
        model = self.catalog.resolve(model_id)
        index_path = str(model.index_path) if model.index_path else ""

        with GPU_LOCK:
            vc = self._load_model(model)
            info, output = vc.vc_single(
                speaker_id,
                str(input_path),
                pitch_shift,
                None,
                f0_method,
                index_path,
                "",
                index_rate if model.index_path else 0.0,
                filter_radius,
                resample_sr,
                rms_mix_rate,
                protect,
            )

        sample_rate, audio = output if output is not None else (None, None)
        if not str(info).startswith("Success.") or sample_rate is None or audio is None:
            logger.error("RVC conversion failed: %s", info)
            raise ConversionFailed("voice conversion failed")
        return int(sample_rate), audio
