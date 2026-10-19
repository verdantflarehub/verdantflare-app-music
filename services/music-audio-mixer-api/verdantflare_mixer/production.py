"""Bounded, offline production DSP. No plugin loading, URLs, or executable presets."""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy import signal

RATE = 48000
ENGINE = "vf-production-1"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(Strict):
    seconds: float = Field(ge=0, le=300)
    value: float = Field(ge=0, le=1)


class Note(Strict):
    start_seconds: float = Field(ge=0, lt=300)
    duration_seconds: float = Field(gt=0, le=30)
    midi: int = Field(ge=24, le=108)
    velocity: float = Field(default=0.7, ge=0, le=1)


class Region(Strict):
    start_seconds: float = Field(default=0, ge=0, lt=300)
    end_seconds: float | None = Field(default=None, gt=0, le=300)

    @model_validator(mode="after")
    def order(self):
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must exceed start_seconds")
        return self


class Tune(Region):
    mode: Literal["scale", "notes"] = "scale"
    pitch_classes: list[int] = Field(
        default_factory=lambda: [0, 2, 4, 5, 7, 9, 11], min_length=1, max_length=12
    )
    notes: list[Note] = Field(default_factory=list, max_length=4096)
    strength: float = Field(default=0.65, ge=0, le=1)
    retune_ms: float = Field(default=45, ge=0, le=500)
    vibrato_preserve: float = Field(default=0.5, ge=0, le=1)
    formant_semitones: float = Field(default=0, ge=-4, le=4)
    min_periodicity: float = Field(default=0.65, ge=0.3, le=0.99)

    @model_validator(mode="after")
    def valid_notes(self):
        if any(n not in range(12) for n in self.pitch_classes):
            raise ValueError("pitch_classes must be 0..11")
        if self.mode == "notes" and not self.notes:
            raise ValueError("notes mode requires a target note timeline")
        last = 0.0
        for n in sorted(self.notes, key=lambda n: n.start_seconds):
            if n.start_seconds < last:
                raise ValueError("target vocal notes must not overlap")
            last = n.start_seconds + n.duration_seconds
        return self


class EQ(Strict):
    kind: Literal["eq"] = "eq"
    highpass_hz: float = Field(default=80, ge=20, le=1000)
    frequency_hz: float = Field(default=3500, ge=100, le=16000)
    gain_db: float = Field(default=2, ge=-12, le=12)
    q: float = Field(default=0.8, ge=0.1, le=10)


class Dynamics(Strict):
    kind: Literal["compressor"] = "compressor"
    threshold_db: float = Field(default=-18, ge=-60, le=0)
    ratio: float = Field(default=3, ge=1, le=20)
    attack_ms: float = Field(default=10, ge=0.1, le=200)
    release_ms: float = Field(default=100, ge=10, le=2000)


class Delay(Strict):
    kind: Literal["delay"] = "delay"
    seconds: float = Field(default=0.12, ge=0.001, le=2)
    feedback: float = Field(default=0.15, ge=0, le=0.8)
    mix: float = Field(default=0.1, ge=0, le=1)


class Reverb(Strict):
    kind: Literal["reverb"] = "reverb"
    room_size: float = Field(default=0.2, ge=0, le=1)
    damping: float = Field(default=0.6, ge=0, le=1)
    mix: float = Field(default=0.08, ge=0, le=1)


class Doubler(Strict):
    kind: Literal["doubler"] = "doubler"
    cents: float = Field(default=7, ge=0, le=25)
    delay_ms: float = Field(default=14, ge=0, le=50)
    mix: float = Field(default=0.15, ge=0, le=1)


class Formant(Strict):
    kind: Literal["formant"] = "formant"
    semitones: float = Field(default=0.5, ge=-4, le=4)
    mix: float = Field(default=0.5, ge=0, le=1)


class Vocoder(Strict):
    kind: Literal["vocoder"] = "vocoder"
    bands: int = Field(default=24, ge=8, le=48)
    attack_ms: float = Field(default=5, ge=1, le=100)
    release_ms: float = Field(default=60, ge=5, le=500)
    unvoiced_mix: float = Field(default=0.15, ge=0, le=1)
    mix: float = Field(default=0.25, ge=0, le=1)


Effect = Annotated[
    EQ | Dynamics | Delay | Reverb | Doubler | Formant | Vocoder,
    Field(discriminator="kind"),
]


class Process(Region):
    effects: list[Effect] = Field(default_factory=list, max_length=12)
    wet: float = Field(default=1, ge=0, le=1)
    wet_automation: list[Point] = Field(default_factory=list, max_length=512)
    tail_seconds: float = Field(default=1, ge=0, le=8)


class Instrument(Strict):
    duration_seconds: float = Field(gt=0, le=300)
    notes: list[Note] = Field(min_length=1, max_length=4096)
    waveform: Literal["sine", "triangle", "saw", "square"] = "saw"
    attack_ms: float = Field(default=5, ge=1, le=5000)
    decay_ms: float = Field(default=160, ge=1, le=5000)
    sustain: float = Field(default=0.2, ge=0, le=1)
    release_ms: float = Field(default=100, ge=1, le=5000)
    cutoff_hz: float = Field(default=6000, ge=80, le=18000)
    cutoff_automation: list[Point] = Field(default_factory=list, max_length=512)
    detune_cents: float = Field(default=4, ge=0, le=30)
    gain_db: float = Field(default=-12, ge=-60, le=0)
    pan: float = Field(default=0, ge=-1, le=1)

    @model_validator(mode="after")
    def bounds(self):
        if any(
            n.start_seconds + n.duration_seconds > self.duration_seconds + 1e-6
            for n in self.notes
        ):
            raise ValueError("note exceeds instrument timeline")
        return self


class Track(Strict):
    gain_db: float = Field(default=0, ge=-60, le=12)
    pan: float = Field(default=0, ge=-1, le=1)
    offset_seconds: float = Field(default=0, ge=0, le=300)
    source_start_seconds: float = Field(default=0, ge=0, le=300)
    source_end_seconds: float | None = Field(default=None, gt=0, le=300)
    gain_automation: list[Point] = Field(default_factory=list, max_length=512)
    effects: list[Effect] = Field(default_factory=list, max_length=12)
    duck_from_track: int | None = Field(default=None, ge=0, le=15)
    duck_db: float = Field(default=6, ge=0, le=18)


class Mix(Strict):
    tracks: list[Track] = Field(min_length=1, max_length=16)
    duration_seconds: float = Field(gt=0, le=300)
    output_gain_db: float = Field(default=-3, ge=-60, le=12)

    @model_validator(mode="after")
    def sidechains(self):
        for i, t in enumerate(self.tracks):
            if t.duck_from_track is not None and (
                t.duck_from_track >= len(self.tracks) or t.duck_from_track == i
            ):
                raise ValueError("invalid sidechain track")
            if (
                t.source_end_seconds is not None
                and t.source_end_seconds <= t.source_start_seconds
            ):
                raise ValueError("invalid source interval")
            if any(isinstance(e, Vocoder) for e in t.effects):
                raise ValueError("render vocoder separately with an explicit carrier")
        return self


MODELS = {
    "vocal.tune": Tune,
    "vocal.process": Process,
    "instrument.render": Instrument,
    "mix.render": Mix,
}


def capabilities():
    return {
        "engine": ENGINE,
        "sample_rate": RATE,
        "schemas": {k: v.model_json_schema() for k, v in MODELS.items()},
        "limits": {"max_audio_seconds": 300, "max_tracks": 16, "max_polyphony": 32},
        "timing": "All events use seconds; vocal note times are relative to the selected source region.",
        "licensing": "Built-in original additive synth; WORLD MIT; scipy BSD; Pedalboard GPL-3.0.",
    }


def read_audio(path):
    try:
        info = sf.info(path)
    except RuntimeError as error:
        raise ValueError("unreadable audio") from error
    if (
        info.channels not in (1, 2)
        or info.duration <= 0
        or info.duration > 300
        or info.samplerate < 16000
        or info.samplerate > 192000
    ):
        raise ValueError(
            "audio must be mono/stereo, 16..192 kHz, and at most 300 seconds"
        )
    x, sr = sf.read(path, dtype="float64", always_2d=True)
    if not np.isfinite(x).all():
        raise ValueError("non-finite audio")
    if sr != RATE:
        g = math.gcd(sr, RATE)
        x = signal.resample_poly(x, RATE // g, sr // g, axis=0)
    return np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x


def region(x, start, end):
    stop = len(x) if end is None else round(end * RATE)
    first = round(start * RATE)
    if first >= len(x) or stop > len(x) or stop <= first:
        raise ValueError("source region is outside audio")
    return x[first:stop].copy()


def automation(points, n, default=1.0):
    if not points:
        return np.full(n, default)
    times = [p.seconds for p in points]
    if any(b <= a for a, b in zip(times, times[1:])) or times[-1] > n / RATE:
        raise ValueError(
            "automation points must be ordered, unique and inside the timeline"
        )
    return np.interp(np.arange(n) / RATE, times, [p.value for p in points])


def stereo_pan(x, pan):
    # Balance preserves an existing stereo image at centre.
    return x * np.array([min(1.0, 1 - pan), min(1.0, 1 + pan)])


def world_transform(x, cfg):
    import pyworld as pw

    mono = np.ascontiguousarray(x.mean(axis=1), dtype=np.float64)
    if len(mono) < RATE // 10:
        raise ValueError("WORLD requires at least 100 ms")
    f0, times = pw.harvest(mono, RATE, f0_floor=65, f0_ceil=1100, frame_period=5)
    sp = pw.cheaptrick(mono, f0, times, RATE)
    ap = pw.d4c(mono, f0, times, RATE)
    periodicity = 1 - ap[:, : max(2, sp.shape[1] // 8)].mean(axis=1)
    voiced = (f0 > 0) & (periodicity >= cfg.min_periodicity)
    midi = np.zeros_like(f0)
    midi[voiced] = 69 + 12 * np.log2(f0[voiced] / 440)
    target = midi.copy()
    if cfg.mode == "notes":
        covered = np.zeros(len(f0), dtype=bool)
        for note in cfg.notes:
            mask = (
                (times >= note.start_seconds)
                & (times < note.start_seconds + note.duration_seconds)
                & voiced
            )
            target[mask] = note.midi
            covered |= mask
        voiced &= covered
    else:
        allowed = np.array([i for i in range(24, 109) if i % 12 in cfg.pitch_classes])
        if voiced.any():
            target[voiced] = allowed[
                np.argmin(abs(midi[voiced, None] - allowed), axis=1)
            ]
    # Smooth correction within voiced segments, never across consonants or rests.
    correction = np.zeros_like(f0)
    alpha = 1 if cfg.retune_ms == 0 else 1 - math.exp(-5 / cfg.retune_ms)
    starts = np.flatnonzero(voiced & ~np.r_[False, voiced[:-1]])
    ends = np.flatnonzero(voiced & ~np.r_[voiced[1:], False]) + 1
    for a, b in zip(starts, ends):
        contour = midi[a:b]
        baseline = signal.medfilt(
            contour, kernel_size=min(21, ((b - a - 1) // 2) * 2 + 1)
        )
        delta = target[a:b] + cfg.vibrato_preserve * (contour - baseline) - contour
        correction[a:b] = (
            signal.lfilter([alpha], [1, -(1 - alpha)], delta) * cfg.strength
        )
    new_f0 = f0 * (2 ** (correction / 12))
    if cfg.formant_semitones:
        bins = np.arange(sp.shape[1])
        source_bins = bins / (2 ** (cfg.formant_semitones / 12))
        sp = np.ascontiguousarray([np.interp(source_bins, bins, row) for row in sp])
    y = pw.synthesize(new_f0, sp, ap, RATE, frame_period=5)[: len(x)]
    y = np.pad(y, (0, max(0, len(x) - len(y))))
    mask = np.interp(np.arange(len(x)) / RATE, times, voiced.astype(float))
    # Five millisecond edges; keep consonants, breaths and original stereo outside voiced intervals.
    mask = np.minimum(mask, np.convolve(mask, np.ones(241) / 241, mode="same"))
    if cfg.strength == 0 and cfg.formant_semitones == 0:
        mask[:] = 0
    wet = x * (1 - mask[:, None]) + y[:, None] * mask[:, None]
    return wet, {
        "voiced_frames": int(voiced.sum()),
        "frame_count": len(f0),
        "median_correction_cents": (
            float(np.median(correction[voiced]) * 100) if voiced.any() else 0
        ),
        "pitch_curve": [
            {
                "seconds": round(float(times[i]), 3),
                "input_hz": round(float(f0[i]), 3),
                "output_hz": round(float(new_f0[i]), 3),
                "periodicity": round(float(periodicity[i]), 3),
            }
            for i in range(0, len(f0), 4)
        ],
    }


def vocode(x, carrier, cfg):
    if carrier is None or len(carrier) != len(x):
        raise ValueError(
            "vocoder requires a carrier matching the selected duration including tail"
        )
    mono = x.mean(axis=1)
    edges = np.geomspace(100, 12000, cfg.bands + 1)
    out = np.zeros_like(x)
    for lo, hi in zip(edges, edges[1:]):
        sos = signal.butter(2, [lo, hi], btype="bandpass", fs=RATE, output="sos")
        mod = signal.sosfilt(sos, mono)
        # Fast envelope plus a slower release follower approximation.
        fast = math.exp(-1 / (RATE * cfg.attack_ms / 1000))
        slow = math.exp(-1 / (RATE * cfg.release_ms / 1000))
        power = signal.lfilter([1 - fast], [1, -fast], mod * mod)
        held = signal.lfilter([1 - slow], [1, -slow], power)
        env = np.sqrt(np.maximum(power, held) + 1e-12)
        band = signal.sosfilt(sos, carrier, axis=0)
        rms = np.sqrt(np.mean(band * band))
        if rms > 1e-8:
            out += band * (env[:, None] / rms)
    high = signal.sosfilt(
        signal.butter(2, 5000, btype="highpass", fs=RATE, output="sos"), x, axis=0
    )
    out += cfg.unvoiced_mix * high
    return out


def effects(x, chain, carrier=None):
    from pedalboard import (
        Pedalboard,
        HighpassFilter,
        PeakFilter,
        Compressor,
        Delay as PBDelay,
        Reverb as PBReverb,
        PitchShift,
    )

    y = x.copy()
    for e in chain:
        if isinstance(e, EQ):
            board = [
                HighpassFilter(e.highpass_hz),
                PeakFilter(e.frequency_hz, e.gain_db, e.q),
            ]
        elif isinstance(e, Dynamics):
            board = [Compressor(e.threshold_db, e.ratio, e.attack_ms, e.release_ms)]
        elif isinstance(e, Delay):
            board = [PBDelay(e.seconds, e.feedback, e.mix)]
        elif isinstance(e, Reverb):
            board = [
                PBReverb(
                    room_size=e.room_size,
                    damping=e.damping,
                    wet_level=e.mix,
                    dry_level=1 - e.mix,
                )
            ]
        elif isinstance(e, Doubler):
            layer = np.zeros_like(y)
            for ch, shift in enumerate((-e.cents / 100, e.cents / 100)):
                z = PitchShift(semitones=shift)(y.mean(axis=1).astype(np.float32), RATE)
                delay = round(e.delay_ms * RATE / 1000)
                if delay:
                    z = np.pad(z, (delay, 0))[: len(y)]
                layer[:, ch] = z
            y = (1 - e.mix) * y + e.mix * layer
            continue
        elif isinstance(e, Formant):
            z, _ = world_transform(y, Tune(strength=0, formant_semitones=e.semitones))
            y = (1 - e.mix) * y + e.mix * z
            continue
        elif isinstance(e, Vocoder):
            y = (1 - e.mix) * y + e.mix * vocode(y, carrier, e)
            continue
        else:
            raise ValueError("unsupported effect")
        y = Pedalboard(board)(y.T.astype(np.float32), RATE).T.astype(np.float64)
    return y


def instrument(cfg):
    n = round(cfg.duration_seconds * RATE)
    y = np.zeros(n)
    occupancy = np.zeros(math.ceil(cfg.duration_seconds * 100) + 2, dtype=int)
    for note in cfg.notes:
        a = round(note.start_seconds * RATE)
        gate = round(note.duration_seconds * RATE)
        release = round(cfg.release_ms * RATE / 1000)
        length = min(gate + release, n - a)
        i, j = int(a / RATE * 100), math.ceil((a + length) / RATE * 100)
        occupancy[i:j] += 1
        if occupancy.max() > 32:
            raise ValueError("maximum polyphony is 32")
        t = np.arange(length) / RATE
        tone = np.zeros(length)
        base = 440 * 2 ** ((note.midi - 69) / 12)
        for detune in (-cfg.detune_cents, cfg.detune_cents):
            freq = base * 2 ** (detune / 1200)
            harmonics = min(64, int((RATE / 2 - 100) / freq))
            for k in range(1, harmonics + 1):
                if cfg.waveform == "sine" and k > 1:
                    break
                if cfg.waveform in {"square", "triangle"} and k % 2 == 0:
                    continue
                amp = (
                    1 / k
                    if cfg.waveform != "triangle"
                    else (-1) ** ((k - 1) // 2) / (k * k)
                )
                tone += amp * np.sin(2 * np.pi * k * freq * t) / 2
        attack = cfg.attack_ms / 1000
        decay = cfg.decay_ms / 1000
        env = np.where(
            t < attack,
            t / attack,
            cfg.sustain + (1 - cfg.sustain) * np.exp(-(t - attack) / decay * 5),
        )
        if gate < length:
            env[gate:] = env[max(0, gate - 1)] * np.linspace(1, 0, length - gate)
        y[a : a + length] += tone * env * note.velocity
    # Filter automation uses smooth crossfades between two bounded lowpasses.
    low = signal.sosfilt(
        signal.butter(2, max(80, cfg.cutoff_hz / 8), fs=RATE, output="sos"), y
    )
    high = signal.sosfilt(signal.butter(2, cfg.cutoff_hz, fs=RATE, output="sos"), y)
    blend = automation(cfg.cutoff_automation, n)
    y = (low * (1 - blend) + high * blend) * 10 ** (cfg.gain_db / 20)
    return stereo_pan(np.repeat(y[:, None], 2, axis=1), cfg.pan)


def render_mix(xs, cfg):
    if len(xs) != len(cfg.tracks):
        raise ValueError("input count must equal track configuration count")
    n = round(cfg.duration_seconds * RATE)
    layers = []
    for source, track in zip(xs, cfg.tracks):
        part = region(source, track.source_start_seconds, track.source_end_seconds)
        offset = round(track.offset_seconds * RATE)
        if offset >= n or len(part) + offset > n + 1:
            raise ValueError("track exceeds mix duration; trim source explicitly")
        layer = np.zeros((n, 2))
        layer[offset : offset + min(len(part), n - offset)] = part[: n - offset]
        layer = effects(layer, track.effects)
        layer = stereo_pan(layer, track.pan) * 10 ** (track.gain_db / 20)
        layer *= automation(track.gain_automation, n)[:, None]
        layers.append(layer)
    out = np.zeros((n, 2))
    for layer, track in zip(layers, cfg.tracks):
        if track.duck_from_track is not None:
            key = layers[track.duck_from_track].mean(axis=1)
            coeff = math.exp(-1 / (RATE * 0.04))
            env = np.sqrt(signal.lfilter([1 - coeff], [1, -coeff], key * key))
            layer = layer * 10 ** (
                -track.duck_db * np.clip(env / 0.1, 0, 1)[:, None] / 20
            )
        out += layer
    return out * 10 ** (cfg.output_gain_db / 20)


def wav_bytes(x):
    if not np.isfinite(x).all():
        raise ValueError("DSP produced non-finite audio")
    # Float stems retain headroom. No hidden limiter or normalization.
    out = io.BytesIO()
    sf.write(
        out, x, RATE, format="WAV", subtype="PCM_24" if np.max(abs(x)) < 1 else "FLOAT"
    )
    return out.getvalue()


def render(operation, config, paths):
    cfg = MODELS[operation].model_validate(config)
    xs = [read_audio(p) for p in paths]
    outputs = {}
    analysis = {}
    if operation == "instrument.render":
        if xs:
            raise ValueError("instrument.render accepts no audio uploads")
        y = instrument(cfg)
    elif operation == "mix.render":
        y = render_mix(xs, cfg)
    else:
        if len(xs) < 1 or len(xs) > 2:
            raise ValueError("expected source and optional carrier")
        dry = region(xs[0], cfg.start_seconds, cfg.end_seconds)
        if operation == "vocal.tune":
            if len(xs) != 1:
                raise ValueError("tuning accepts only one source")
            y, analysis = world_transform(dry, cfg)
        else:
            dry = np.pad(dry, ((0, round(cfg.tail_seconds * RATE)), (0, 0)))
            carrier = xs[1] if len(xs) == 2 else None
            if carrier is not None:
                if len(carrier) > len(dry):
                    raise ValueError("carrier exceeds selected duration plus tail")
                carrier = np.pad(carrier, ((0, len(dry) - len(carrier)), (0, 0)))
            wet = effects(dry, cfg.effects, carrier)
            amount = automation(cfg.wet_automation, len(dry), cfg.wet)[:, None]
            y = dry * (1 - amount) + wet * amount
            outputs["wet.wav"] = wav_bytes(wet)
        outputs["dry.wav"] = wav_bytes(dry)
    outputs["render.wav"] = wav_bytes(y)
    peak = float(np.max(abs(y)))
    report = {
        "engine": ENGINE,
        "operation": operation,
        "sample_rate": RATE,
        "duration_seconds": len(y) / RATE,
        "config": cfg.model_dump(),
        "peak_dbfs": 20 * math.log10(max(peak, 1e-12)),
        "rms_dbfs": 20 * math.log10(max(float(np.sqrt(np.mean(y * y))), 1e-12)),
        "warnings": (
            ["float output exceeds 0 dBFS; reduce mix gain before PCM export"]
            if peak >= 1
            else []
        ),
        "analysis": analysis,
        "inputs_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths],
        "outputs_sha256": {
            k: hashlib.sha256(v).hexdigest() for k, v in outputs.items()
        },
    }
    outputs["report.json"] = json.dumps(
        report, ensure_ascii=False, allow_nan=False
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, payload in outputs.items():
            z.writestr(name, payload)
    return buf.getvalue()
