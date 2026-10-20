"""Source-conditioned end continuation. Original PCM frames are immutable."""
from __future__ import annotations

import audioop
import hashlib
import io
import json
import math
import wave

from .executor import ExecutionError, open_pcm_wav


def source_pcm(payload):
    try:
        with open_pcm_wav(payload) as src:
            params = src.getparams()
            frames = src.readframes(params.nframes)
    except (EOFError, wave.Error) as error:
        raise ValueError("continuation source must be PCM WAV") from error
    if (params.comptype != "NONE" or params.sampwidth not in {2, 3}
            or params.nchannels not in {1, 2} or not 8000 <= params.framerate <= 192000
            or params.nframes <= 0
            or len(frames) != params.nframes * params.sampwidth * params.nchannels):
        raise ValueError("continuation source must be complete mono/stereo PCM16/24 WAV")
    return params, frames


def validate(executor, project_id, asset_id, config):
    if set(config) != {"addition_seconds", "instructions", "lyrics", "seed"}:
        raise ValueError("invalid continuation fields")
    seconds = config["addition_seconds"]
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or not 3 <= seconds <= 60):
        raise ValueError("addition_seconds must be finite and between 3 and 60")
    seed = config["seed"]
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError("invalid continuation seed")
    if not isinstance(config["instructions"], str) or not config["instructions"].strip():
        raise ValueError("continuation instructions are required")
    if not isinstance(config["lyrics"], str):
        raise ValueError("continuation lyrics must be text")
    if len(config["instructions"]) > 16000 or len(config["lyrics"]) > 16000:
        raise ValueError("continuation text is too long")
    source, raw = executor.store.read(asset_id, project_id)
    params, frames = source_pcm(raw)
    if params.nframes / params.framerate + seconds > 300:
        raise ValueError("continuation total duration must not exceed 300 seconds")
    if (executor.service_urls.music_editor is None
            or executor.service_urls.music_editor_backend != "ace_step_v1_5"):
        raise ValueError("continuation requires ACE-Step 1.5 editor")
    return source, raw, params, frames


def append_generated(source, edited, addition_seconds):
    params, original = source_pcm(source)
    ep, generated = source_pcm(edited)
    if ep.sampwidth != params.sampwidth:
        generated = audioop.lin2lin(generated, ep.sampwidth, params.sampwidth)
    if ep.nchannels != params.nchannels:
        generated = (audioop.tostereo(generated, params.sampwidth, 1, 1)
                     if ep.nchannels == 1 else audioop.tomono(generated, params.sampwidth, .5, .5))
    if ep.framerate != params.framerate:
        generated, _ = audioop.ratecv(generated, params.sampwidth, params.nchannels,
                                      ep.framerate, params.framerate, None)
    frame_bytes = params.nchannels * params.sampwidth
    requested_frames = params.nframes + round(addition_seconds * params.framerate)
    available_frames = len(generated) // frame_bytes
    # ACE-Step's 25 Hz latent grid rounds the end down by less than 40 ms.
    # Preserve its real end; never fabricate missing PCM frames.
    if available_frames < requested_frames - math.ceil(params.framerate / 25):
        raise ExecutionError("continuation result is shorter than requested; padding is forbidden")
    total_frames = min(requested_frames, available_frames)
    tail = generated[len(original):total_frames * frame_bytes]
    if audioop.rms(tail, params.sampwidth) == 0:
        raise ExecutionError("continuation returned silence")
    result = io.BytesIO()
    with wave.open(result, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(original + tail)
    return result.getvalue(), {
        "source_frames": params.nframes, "output_frames": total_frames,
        "requested_frames": requested_frames,
        "duration_shortfall_seconds": (requested_frames - total_frames) / params.framerate,
        "sample_rate": params.framerate, "channels": params.nchannels,
        "sample_width": params.sampwidth,
        "preserved_prefix_pcm_sha256": hashlib.sha256(original).hexdigest(),
        "prefix_unchanged": True,
        "musical_review": "unreviewed",
    }


def render(executor, project_id, asset_id, config, on_submitted, resume_task_id=None):
    if resume_task_id is None:
        raise ExecutionError("ACE-Step song extension is retired; only recorded-task recovery is allowed")
    source, raw, params, _ = validate(executor, project_id, asset_id, config)
    duration = params.nframes / params.framerate
    edited, task_id = executor._conditioned_repaint(
        source, raw, duration, duration + config["addition_seconds"],
        config["instructions"], config["lyrics"], config["seed"],
        0.05, "aggressive", 1.0, on_submitted=on_submitted, resume_task_id=resume_task_id,
        extra_data={"audio_duration": str(duration + config["addition_seconds"]),
                    "inference_steps": "48", "thinking": "false", "batch_size": "1"},
    )
    output, report = append_generated(raw, edited, config["addition_seconds"])
    report.update({
        "operation": "music.extend", "backend": "ace_step_v1_5_repaint_outpaint",
        "backend_task_id": task_id, "source_artifact_id": asset_id,
        "source_sha256": hashlib.sha256(raw).hexdigest(), "config": config,
        "outputs_sha256": {"render.wav": hashlib.sha256(output).hexdigest()},
        "warnings": ["Prefix integrity is not proof of musical continuity; audition required."],
    })
    return {"render.wav": output, "report.json": json.dumps(report, ensure_ascii=False).encode()}
