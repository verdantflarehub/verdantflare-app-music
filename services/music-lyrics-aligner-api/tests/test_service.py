from __future__ import annotations

import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from verdantflare_lyrics_aligner.service import (
    AlignmentFailed,
    AlignmentService,
    InvalidAlignmentInput,
    ModelUnavailable,
    format_lrc,
    parse_lyrics,
)


def segment(start: float, end: float) -> object:
    return types.SimpleNamespace(words=[types.SimpleNamespace(start=start, end=end)])


class AlignmentServiceTest(unittest.TestCase):
    def test_parses_utf8_lyrics_and_rejects_timestamps_and_labels(self) -> None:
        self.assertEqual(parse_lyrics("第一句\r\n\r\n第二句\n".encode()), ["第一句", "第二句"])
        for payload in (b"", b"[00:01.000]line", b"[Chorus]\nline", b"\xff"):
            with self.subTest(payload=payload), self.assertRaises(InvalidAlignmentInput):
                parse_lyrics(payload)

    def test_formats_exact_lines_with_strict_millisecond_timestamps(self) -> None:
        self.assertEqual(
            format_lrc(["第一句", "第二句"], [segment(1.2344, 2), segment(62.3456, 64)], 70),
            "[00:01.234]第一句\n[01:02.346]第二句\n",
        )

    def test_rejects_incomplete_non_increasing_or_out_of_range_alignment(self) -> None:
        invalid = (
            ([segment(1, 2)], 10),
            ([segment(1, 2), segment(1.0001, 3)], 10),
            ([segment(1, 2), segment(11, 12)], 10),
            ([segment(1, 1), segment(2, 3)], 10),
        )
        for segments, duration in invalid:
            with self.subTest(segments=segments), self.assertRaises(AlignmentFailed):
                format_lrc(["一", "二"], segments, duration)

    def test_load_requires_cuda_and_uses_persistent_download_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = AlignmentService(Path(directory), cuda_available=lambda: False)
            with self.assertRaises(ModelUnavailable):
                service.load()

            calls = []
            model = object()
            service = AlignmentService(
                Path(directory),
                model_loader=lambda *args, **kwargs: calls.append((args, kwargs)) or model,
                cuda_available=lambda: True,
            )
            service.load()
            self.assertTrue(service.ready)
            self.assertEqual(calls, [(('small',), {'device': 'cuda', 'download_root': directory})])

    def test_align_preserves_lines_and_uses_known_text_contract(self) -> None:
        calls = []

        class FakeModel:
            def align(self, *args, **kwargs):
                calls.append((args, kwargs))
                return types.SimpleNamespace(segments=[segment(0.5, 1.5), segment(2.0, 3.0)])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.wav"
            source.touch()
            decoded = root / "vocal-16k.wav"
            with wave.open(str(decoded), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\0\0" * 16000 * 4)
            service = AlignmentService(root, model_loader=lambda *args, **kwargs: FakeModel(), cuda_available=lambda: True)
            service.load()
            with patch.object(service, "_decode_audio", return_value=4.0):
                output = service.align(source, "第一句\n第二句\n".encode(), root)

        self.assertEqual(output, "[00:00.500]第一句\n[00:02.000]第二句\n")
        self.assertEqual(calls[0][0][1], "第一句\n第二句")
        self.assertEqual(
            calls[0][1],
            {
                "language": "zh",
                "original_split": True,
                "failure_threshold": 0.0,
                "verbose": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
