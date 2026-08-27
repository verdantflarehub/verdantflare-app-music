from __future__ import annotations

import contextlib

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .contracts import Invocation, invocation


mcp = MCPServer("VerdantFlare Music")


@mcp.tool(name="music.generate")
def music_generate(
    lyrics: str,
    instructions: str,
    seed: int = 7,
    max_new_tokens: int = 9000,
) -> Invocation:
    """Build a MiniMax Music 3 generation invocation for an approved lyric plan."""
    if not lyrics.strip() or not instructions.strip():
        raise ValueError("lyrics and instructions are required")
    if not 1 <= max_new_tokens <= 9000:
        raise ValueError("max_new_tokens must be between 1 and 9000")
    return invocation(
        "music.generate",
        "music",
        "/v1/audio/speech",
        {
            "model": "MiniMaxAI/MiniMax-Music3",
            "input": lyrics,
            "instructions": instructions,
            "seed": seed,
            "max_new_tokens": max_new_tokens,
            "response_format": "wav",
            "stream": False,
        },
        {},
        [("generated.wav", "audio/wav")],
        encoding="application/json",
    )


@mcp.tool(name="stems.separate")
def stems_separate(audio_asset_id: str) -> Invocation:
    """Build a vocal/instrumental separation invocation for a Station audio asset."""
    return invocation(
        "stems.separate",
        "stems",
        "/v1/audio/stem-separations",
        {},
        {"audio": audio_asset_id},
        [
            ("instrumental.wav", "audio/wav"),
            ("vocal_dry_original.wav", "audio/wav"),
        ],
    )


@mcp.tool(name="voice.train")
def voice_train(
    audio_asset_id: str,
    model_id: str,
    epochs: int = 200,
    batch_size: int = 4,
) -> Invocation:
    """Build an RVC v2 training invocation for a Station recording asset."""
    return invocation(
        "voice.train",
        "voice",
        "/v1/voice-models/train",
        {"model_id": model_id, "epochs": epochs, "batch_size": batch_size},
        {"audio": audio_asset_id},
        [
            (f"{model_id}.pth", "application/octet-stream"),
            (f"{model_id}.index", "application/octet-stream"),
            (f"{model_id}_validation.wav", "audio/wav"),
        ],
    )


@mcp.tool(name="voice.convert")
def voice_convert(audio_asset_id: str, model_id: str, pitch_shift: int = 0) -> Invocation:
    """Build an RVC voice conversion invocation using a controlled model ID."""
    return invocation(
        "voice.convert",
        "voice",
        "/v1/audio/voice-conversions",
        {"model_id": model_id, "pitch_shift": pitch_shift, "f0_method": "rmvpe"},
        {"audio": audio_asset_id},
        [("vocal_dry_cloned.wav", "audio/wav")],
    )


@mcp.tool(name="mix.master")
def mix_master(
    instrumental_asset_id: str,
    vocal_asset_id: str,
    lyrics_lrc_asset_id: str,
    bpm: float,
) -> Invocation:
    """Build a mastering invocation for approved Station audio and LRC assets."""
    return invocation(
        "mix.master",
        "mix",
        "/v1/audio/masters",
        {"bpm": bpm},
        {
            "instrumental": instrumental_asset_id,
            "vocal": vocal_asset_id,
            "lyrics_lrc": lyrics_lrc_asset_id,
        },
        [
            ("Final_Song_Master.wav", "audio/wav"),
            ("Final_Song.mp3", "audio/mpeg"),
            ("Final_Song.lrc", "text/plain"),
        ],
    )


async def health(request) -> JSONResponse:
    return JSONResponse({"status": "ok", "contract_version": "v1-draft"})


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health),
        Mount(
            "/",
            app=mcp.streamable_http_app(json_response=True, stateless_http=True),
        ),
    ],
    lifespan=lifespan,
)
