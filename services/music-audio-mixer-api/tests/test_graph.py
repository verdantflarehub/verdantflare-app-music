from __future__ import annotations

import array
import math
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from verdantflare_mixer.graph import build_mix_graph, validate_arrangement


class VocalGraphTest(unittest.TestCase):
    def test_mode_requirements(self) -> None:
        validate_arrangement("solo", 0, [])
        validate_arrangement("duet", 1, [-3.0])
        validate_arrangement("choir", 2, [-6.0, -6.0])
        with self.assertRaisesRegex(ValueError, "duet requires 1"):
            validate_arrangement("duet", 0, [])
        with self.assertRaisesRegex(ValueError, "choir requires 2 to 7"):
            validate_arrangement("choir", 1, [-3.0])
        with self.assertRaisesRegex(ValueError, "must match"):
            validate_arrangement("choir", 2, [-3.0])

    def test_legacy_graph_keeps_two_inputs(self) -> None:
        self.assertIn("amix=inputs=2", build_mix_graph())

    def test_duet_graph_uses_three_inputs(self) -> None:
        graph = build_mix_graph(additional_gains_db=(-3.0,))
        self.assertIn("[2:a]volume=-3.0dB[part1]", graph)
        self.assertIn("[1:a][part1]amix=inputs=2:duration=longest:normalize=0[vocalbus]", graph)
        self.assertIn("[vocalbus]asplit=2[key][voices]", graph)
        self.assertIn("[0:a][key]sidechaincompress", graph)

    def test_choir_graph_uses_four_inputs(self) -> None:
        graph = build_mix_graph(additional_gains_db=(-6.0, -6.0))
        self.assertIn("[2:a]volume=-6.0dB[part1]", graph)
        self.assertIn("[3:a]volume=-6.0dB[part2]", graph)
        self.assertIn("[1:a][part1][part2]amix=inputs=3:duration=longest:normalize=0[vocalbus]", graph)

    def test_duet_with_backing_uses_distinct_inputs(self) -> None:
        graph = build_mix_graph(backing_gain_db=-6.0, additional_gains_db=(-3.0,))
        self.assertIn("[2:a]volume=-6.0dB[backing]", graph)
        self.assertIn("[3:a]volume=-3.0dB[part1]", graph)
        self.assertIn("[1:a][backing][part1]amix=inputs=3", graph)

    def test_synthetic_duet_and_choir_render(self) -> None:
        ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")

        def render_input(path: Path, frequency: int | None, *, delay_ms: int = 0, duration: int = 3) -> None:
            source = (
                f"sine=frequency={frequency}:sample_rate=48000:duration={duration if delay_ms == 0 else 1}"
                if frequency is not None
                else "anullsrc=channel_layout=stereo:sample_rate=48000:duration=3"
            )
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source]
            if frequency is not None and delay_ms:
                command += ["-af", f"adelay={delay_ms},apad=pad_dur=1"]
            elif frequency is not None and duration == 1:
                command += ["-af", "apad=pad_dur=2"]
            command += ["-t", "3", "-c:a", "pcm_s16le", str(path)]
            subprocess.run(command, check=True)

        def mix(paths: list[Path], gains: tuple[float, ...], output: Path, backing_gain: float | None = None) -> None:
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            for path in paths:
                command += ["-i", str(path)]
            command += ["-filter_complex", build_mix_graph(backing_gain, gains), "-map", "[mix]", "-c:a", "pcm_s16le", str(output)]
            subprocess.run(command, check=True)

        def rms_by_second(path: Path) -> list[float]:
            with wave.open(str(path), "rb") as audio:
                self.assertEqual(audio.getframerate(), 48000)
                self.assertEqual(audio.getnchannels(), 2)
                values = []
                for _ in range(3):
                    samples = array.array("h")
                    samples.frombytes(audio.readframes(48000))
                    values.append(math.sqrt(sum(value * value for value in samples) / len(samples)))
                return values

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instrumental = root / "instrumental.wav"
            first = root / "first.wav"
            second = root / "second.wav"
            third = root / "third.wav"
            duet = root / "duet.wav"
            choir = root / "choir.wav"
            backing = root / "backing.wav"
            duet_with_backing = root / "duet-with-backing.wav"
            render_input(instrumental, None)
            render_input(first, 440, duration=1)
            render_input(second, 660, delay_ms=1000)
            mix([instrumental, first, second], (-3.0,), duet)
            first_second, second_second, silent_second = rms_by_second(duet)
            self.assertGreater(first_second, 100)
            self.assertGreater(second_second, 100)
            self.assertLess(silent_second, 5)

            render_input(backing, 220)
            mix([instrumental, first, backing, second], (-3.0,), duet_with_backing, -6.0)
            self.assertTrue(all(value > 100 for value in rms_by_second(duet_with_backing)))

            render_input(first, 440)
            render_input(second, 660)
            render_input(third, 880)
            mix([instrumental, first, second, third], (-6.0, -6.0), choir)
            self.assertTrue(all(value > 100 for value in rms_by_second(choir)))


if __name__ == "__main__":
    unittest.main()
