from __future__ import annotations

import hashlib
import json
import math
import struct
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from verdantflare_rvc.preparation import PreparationFailed, PreparationSource, VoiceDatasetPreparer


SAMPLE_RATE = 40000


def write_fixture(path: Path, spans: list[tuple[float, int]]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        phase = 0
        for seconds, amplitude in spans:
            frame_count = round(seconds * SAMPLE_RATE)
            samples = []
            for _ in range(frame_count):
                value = 0 if amplitude == 0 else round(amplitude * math.sin(2 * math.pi * 220 * phase / SAMPLE_RATE))
                samples.append(value)
                phase += 1
            output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


class VoiceDatasetPreparerTest(unittest.TestCase):
    def test_rejects_all_silence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "silence.wav"
            write_fixture(source, [(2.0, 0)])
            with self.assertRaisesRegex(PreparationFailed, "no audio"):
                VoiceDatasetPreparer().prepare(
                    [PreparationSource("silence.wav", source, hashlib.sha256(source.read_bytes()).hexdigest())],
                    root / "work",
                    root / "result.zip",
                )

    def test_prepares_pcm_and_removes_only_long_silence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.wav"
            write_fixture(
                source,
                [
                    (0.5, 30000),
                    (0.5, 0),
                    (0.5, 30000),
                    (0.6, 0),
                    (0.1, 30000),
                    (0.6, 0),
                    (0.5, 30000),
                    (1.4, 0),
                    (0.5, 30000),
                ],
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            archive = root / "result.zip"
            metrics = {
                "integrated_lufs": -20.0,
                "true_peak_dbtp": -2.0,
                "loudness_range_lu": 3.0,
            }
            pitch = {
                "reliable_voiced_seconds": 1.0,
                "f0_percentiles_hz": {"p50": 220.0},
                "high_pitch_seconds": {"G4": 0.0},
            }

            with (
                patch("verdantflare_rvc.preparation._probe", return_value={"format": {"duration": "5.2"}}),
                patch("verdantflare_rvc.preparation._loudness", return_value=metrics),
                patch("verdantflare_rvc.preparation._pitch", return_value=pitch),
            ):
                VoiceDatasetPreparer().prepare(
                    [PreparationSource("recording.wav", source, digest)],
                    root / "work",
                    archive,
                )

            with zipfile.ZipFile(archive) as result:
                self.assertEqual(
                    set(result.namelist()),
                    {
                        "prepared-01.wav",
                        "voice-training.wav",
                        "voice-segments.zip",
                        "voice-preparation-report.json",
                        "manifest.json",
                    },
                )
                report = json.loads(result.read("voice-preparation-report.json"))
                prepared = result.read("prepared-01.wav")
                training = result.read("voice-training.wav")
                self.assertEqual(prepared, training)
                self.assertEqual(report["sources"][0]["source_sha256"], digest)
                self.assertEqual(len(report["sources"][0]["segments"]), 3)
                self.assertLessEqual(report["sources"][0]["maximum_silence_seconds"], 1.0)
                self.assertAlmostEqual(report["sources"][0]["prepared_duration_seconds"], 4.1, places=1)
                self.assertLessEqual(report["sources"][0]["sample_peak_dbfs"], -1.99)
                self.assertTrue(report["review_required"])

                segment_duration = sum(
                    item["duration_seconds"] for item in report["sources"][0]["segments"]
                )
                self.assertAlmostEqual(segment_duration, report["training"]["duration_seconds"], places=3)
                with zipfile.ZipFile(__import__("io").BytesIO(result.read("voice-segments.zip"))) as segments:
                    self.assertEqual(
                        set(segments.namelist()),
                        {"01-001.wav", "01-002.wav", "01-003.wav", "manifest.json"},
                    )


if __name__ == "__main__":
    unittest.main()
