from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field


SERVICE_URLS = {
    "music": os.environ.get("MUSIC3_URL", "http://music-minimax-music3-api:8000"),
    "stems": os.environ.get("UVR5_URL", "http://music-uvr5-api:8000"),
    "voice": os.environ.get("RVC_URL", "http://music-rvc-api:8000"),
    "mix": os.environ.get("MIXER_URL", "http://music-audio-mixer-api:8000"),
}


class Artifact(BaseModel):
    name: str
    media_type: str


class Invocation(BaseModel):
    contract_version: str = "v1-draft"
    operation: str
    service_url: str
    method: str = "POST"
    path: str
    encoding: str
    fields: dict[str, Any]
    asset_inputs: dict[str, str]
    outputs: list[Artifact]
    station_resolves_assets: bool = True


def require_asset_id(asset_id: str) -> str:
    value = asset_id.strip()
    if not value or len(value) > 256:
        raise ValueError("asset_id must contain 1-256 characters")
    return value


def invocation(
    operation: str,
    service: str,
    path: str,
    fields: dict[str, Any],
    asset_inputs: dict[str, str],
    outputs: list[tuple[str, str]],
    encoding: str = "multipart/form-data",
) -> Invocation:
    return Invocation(
        operation=operation,
        service_url=SERVICE_URLS[service],
        path=path,
        encoding=encoding,
        fields=fields,
        asset_inputs={name: require_asset_id(value) for name, value in asset_inputs.items()},
        outputs=[Artifact(name=name, media_type=media_type) for name, media_type in outputs],
    )
