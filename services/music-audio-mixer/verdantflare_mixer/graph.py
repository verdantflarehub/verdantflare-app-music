from __future__ import annotations

import math


def validate_arrangement(mode: str, additional_count: int, gains_db: list[float]) -> None:
    required = {"solo": 0, "duet": 1}
    if mode in required and additional_count != required[mode]:
        raise ValueError(f"{mode} requires {required[mode]} additional vocal tracks")
    if mode == "choir" and not 2 <= additional_count <= 7:
        raise ValueError("choir requires 2 to 7 additional vocal tracks")
    if mode not in {"solo", "duet", "choir"}:
        raise ValueError("vocal_mode must be solo, duet, or choir")
    if len(gains_db) != additional_count:
        raise ValueError("additional vocal gains must match the number of tracks")
    if any(not math.isfinite(gain) or not -24.0 <= gain <= 6.0 for gain in gains_db):
        raise ValueError("additional vocal gains must be between -24 and 6 dB")


def build_mix_graph(backing_gain_db: float | None = None, additional_gains_db: tuple[float, ...] = ()) -> str:
    if backing_gain_db is not None and (not math.isfinite(backing_gain_db) or not -24.0 <= backing_gain_db <= 6.0):
        raise ValueError("backing gain must be between -24 and 6 dB")
    if any(not math.isfinite(gain) or not -24.0 <= gain <= 6.0 for gain in additional_gains_db):
        raise ValueError("additional vocal gains must be between -24 and 6 dB")

    duck = "sidechaincompress=threshold=0.04:ratio=3:attack=20:release=250"
    stages: list[str] = []
    labels = ["[1:a]"]
    input_index = 2
    if backing_gain_db is not None:
        stages.append(f"[{input_index}:a]volume={backing_gain_db}dB[backing]")
        labels.append("[backing]")
        input_index += 1
    for number, gain in enumerate(additional_gains_db, start=1):
        label = f"part{number}"
        stages.append(f"[{input_index}:a]volume={gain}dB[{label}]")
        labels.append(f"[{label}]")
        input_index += 1
    if additional_gains_db:
        stages.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0[vocalbus]")
        stages.append("[vocalbus]asplit=2[key][voices]")
        stages.append(f"[0:a][key]{duck}[ducked]")
        stages.append("[ducked][voices]amix=inputs=2:duration=longest:normalize=0[mix]")
    else:
        stages.insert(0, f"[0:a][1:a]{duck}[ducked]")
        stages.append(f"[ducked]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=longest:normalize=0[mix]")
    return ";".join(stages)
