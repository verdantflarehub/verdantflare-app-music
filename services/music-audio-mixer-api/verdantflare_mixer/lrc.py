from __future__ import annotations

import re


TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))\]")


class InvalidLRC(ValueError):
    pass


def validate_lrc(content: bytes) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidLRC("lyrics_lrc must be UTF-8") from error

    timestamps: list[int] = []
    for match in TIMESTAMP.finditer(text):
        minutes, seconds, fraction = match.groups()
        if int(seconds) >= 60:
            raise InvalidLRC("LRC seconds must be between 00 and 59")
        fraction_ms = int(fraction.ljust(3, "0"))
        timestamps.append((int(minutes) * 60 + int(seconds)) * 1000 + fraction_ms)
    if not timestamps:
        raise InvalidLRC("lyrics_lrc must contain at least one timestamp")
    if any(current < previous for previous, current in zip(timestamps, timestamps[1:])):
        raise InvalidLRC("LRC timestamps must be monotonic")
    return text
