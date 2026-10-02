from __future__ import annotations

import audioop
import io
import json
import math
import struct
import wave
from typing import Literal

from pydantic import BaseModel


Voice = Literal["female", "male", "both", "instrumental"]
Section = Literal["Intro", "Verse", "Pre-Chorus", "Chorus", "Bridge", "Outro"]


class DuetLine(BaseModel):
    section: Section
    voice: Voice
    text: str = ""
    bars: int = 2


def build_duet_plan(
    lines: list[DuetLine], *, bpm: float, style: str, candidate_number: int
) -> dict[str, object]:
    if not math.isfinite(bpm) or not 60 <= bpm <= 220 or not float(bpm).is_integer():
        raise ValueError("duet BPM must be an integer between 60 and 220")
    if not 1 <= candidate_number <= 99:
        raise ValueError("candidate_number must be between 1 and 99")
    if not 20 <= len(style.strip()) <= 2000:
        raise ValueError("style must contain 20-2000 characters")
    if not 3 <= len(lines) <= 160:
        raise ValueError("duet plan must contain 3-160 lines")

    seen = {"female": 0, "male": 0, "both": 0}
    planned_lines: list[dict[str, object]] = []
    elapsed_bars = 0
    for number, line in enumerate(lines, start=1):
        text = line.text.strip()
        if not 1 <= line.bars <= 8:
            raise ValueError(f"duet line {number} bars must be between 1 and 8")
        if line.voice == "instrumental":
            if text:
                raise ValueError(f"instrumental duet line {number} must have no lyrics")
        elif not text or len(text) > 100 or "\n" in text or "[" in text or "]" in text:
            raise ValueError(f"duet line {number} must contain 1-100 plain lyric characters")
        if line.voice in seen:
            seen[line.voice] += 1
        planned_lines.append(
            {
                "section": line.section,
                "voice": line.voice,
                "text": text,
                "bars": line.bars,
                "start_seconds": round(elapsed_bars * 240 / bpm, 6),
                "end_seconds": round((elapsed_bars + line.bars) * 240 / bpm, 6),
            }
        )
        elapsed_bars += line.bars
    if min(seen.values()) == 0:
        raise ValueError("duet plan requires female, male, and both vocal lines")
    duration = elapsed_bars * 240 / bpm
    if not 10 <= duration <= 300:
        raise ValueError("duet duration must be between 10 and 300 seconds")
    return {
        "schema_version": 1,
        "candidate_number": candidate_number,
        "bpm": bpm,
        "style": style.strip(),
        "duration_seconds": round(duration, 6),
        "lines": planned_lines,
        "review_status": "unreviewed",
    }


def read_duet_plan(payload: bytes) -> dict[str, object]:
    try:
        plan = json.loads(payload)
        lines = [DuetLine.model_validate(line) for line in plan["lines"]]
        expected = build_duet_plan(
            lines,
            bpm=plan["bpm"],
            style=plan["style"],
            candidate_number=plan["candidate_number"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("duet plan artifact is invalid") from error
    if plan != expected:
        raise ValueError("duet plan artifact does not match its timeline")
    return plan


def lyrics_for_voice(plan: dict[str, object], voice: Literal["female", "male"]) -> str:
    output: list[str] = []
    section = None
    for line in plan["lines"]:
        if line["section"] != section:
            section = line["section"]
            output.append(f"[{section}]")
        output.append(line["text"] if line["voice"] in {voice, "both"} else "[Instrumental]")
    return "\n".join(output)


def pcm16_wav(payload: bytes) -> tuple[wave._wave_params, bytes]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as audio:
            params = audio.getparams()
            if params.comptype != "NONE" or params.sampwidth != 2 or params.nchannels not in {1, 2}:
                raise ValueError("duet audio must be mono or stereo 16-bit PCM WAV")
            frames = audio.readframes(params.nframes)
    except (EOFError, wave.Error) as error:
        raise ValueError("duet audio must be a valid PCM WAV") from error
    if not frames or len(frames) != params.nframes * params.nchannels * 2:
        raise ValueError("duet audio is empty or truncated")
    return params, frames


def wav_bytes(params: wave._wave_params, frames: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(params.nchannels)
        audio.setsampwidth(2)
        audio.setframerate(params.framerate)
        audio.writeframes(frames)
    return output.getvalue()


def validate_vocal_isolation(backing: bytes, vocal: bytes, plan: dict[str, object], voice: str) -> None:
    backing_params, backing_frames = pcm16_wav(backing)
    vocal_params, vocal_frames = pcm16_wav(vocal)
    if (backing_params.framerate, backing_params.nchannels) != (vocal_params.framerate, vocal_params.nchannels):
        raise ValueError("duet vocal format does not match instrumental")
    if abs(backing_params.nframes - vocal_params.nframes) > backing_params.framerate / 4:
        raise ValueError("duet vocal duration differs from instrumental by over 0.25 seconds")

    frame_size = backing_params.nchannels * 2
    for line in plan["lines"]:
        if line["voice"] != voice:
            continue
        start = round(line["start_seconds"] * backing_params.framerate)
        end = min(backing_params.nframes, vocal_params.nframes,
                  round(line["end_seconds"] * backing_params.framerate))
        samples = []
        for frame in range(start, end, max(1, backing_params.framerate // 1000)):
            offset = frame * frame_size
            samples.append((struct.unpack_from("<h", backing_frames, offset)[0],
                            struct.unpack_from("<h", vocal_frames, offset)[0]))
        if not samples:
            raise ValueError(f"generated {voice} duet vocal has an empty solo interval")
        backing_mean = sum(pair[0] for pair in samples) / len(samples)
        vocal_mean = sum(pair[1] for pair in samples) / len(samples)
        backing_energy = sum((pair[0] - backing_mean) ** 2 for pair in samples)
        vocal_energy = sum((pair[1] - vocal_mean) ** 2 for pair in samples)
        if sum(pair[1] ** 2 for pair in samples) / len(samples) < 8 ** 2:
            raise ValueError(f"generated {voice} duet vocal is silent in a solo interval")
        if backing_energy and vocal_energy:
            overlap = sum((pair[0] - backing_mean) * (pair[1] - vocal_mean) for pair in samples)
            if abs(overlap) / math.sqrt(backing_energy * vocal_energy) > 0.75:
                raise ValueError(f"generated {voice} duet vocal contains the instrumental in a solo interval")


def fit_vocal_to_backing(backing: bytes, vocal: bytes, plan: dict[str, object], voice: str) -> bytes:
    backing_params, _ = pcm16_wav(backing)
    vocal_params, vocal_frames = pcm16_wav(vocal)
    if (
        backing_params.framerate != vocal_params.framerate
        or backing_params.nchannels != vocal_params.nchannels
    ):
        raise ValueError("duet vocal format does not match instrumental")
    if abs(backing_params.nframes - vocal_params.nframes) > backing_params.framerate / 4:
        raise ValueError("duet vocal duration differs from instrumental by over 0.25 seconds")
    frame_size = backing_params.nchannels * 2
    target_bytes = backing_params.nframes * frame_size
    vocal_frames = vocal_frames[:target_bytes].ljust(target_bytes, b"\0")
    masked = bytearray(target_bytes)
    fade_frames = max(1, round(0.01 * backing_params.framerate))
    for line in plan["lines"]:
        if line["voice"] not in {voice, "both"}:
            continue
        start = max(0, min(backing_params.nframes, round(line["start_seconds"] * backing_params.framerate)))
        end = max(start, min(backing_params.nframes, round(line["end_seconds"] * backing_params.framerate)))
        masked[start * frame_size : end * frame_size] = vocal_frames[start * frame_size : end * frame_size]
        edge = min(fade_frames, (end - start) // 2)
        for offset in range(edge):
            for position, gain in ((start + offset, offset / edge), (end - offset - 1, offset / edge)):
                for channel in range(backing_params.nchannels):
                    byte = position * frame_size + channel * 2
                    sample = struct.unpack_from("<h", masked, byte)[0]
                    struct.pack_into("<h", masked, byte, round(sample * gain))
    if audioop.rms(masked, 2) < 8:
        raise ValueError(f"generated {voice} duet vocal has no audible content")
    return wav_bytes(backing_params, masked)


def preview_duet(backing: bytes, female: bytes, male: bytes) -> bytes:
    params, instrumental_frames = pcm16_wav(backing)
    female_params, female_frames = pcm16_wav(female)
    male_params, male_frames = pcm16_wav(male)
    for part in (female_params, male_params):
        if (part.framerate, part.nchannels, part.nframes) != (
            params.framerate,
            params.nchannels,
            params.nframes,
        ):
            raise ValueError("duet preview tracks must share one timeline and PCM format")
    if female_frames == male_frames:
        raise ValueError("duet vocal tracks must be independent")
    vocals = audioop.add(audioop.mul(female_frames, 2, 0.27), audioop.mul(male_frames, 2, 0.27), 2)
    mixed = audioop.add(audioop.mul(instrumental_frames, 2, 0.45), vocals, 2)
    return wav_bytes(params, mixed)
