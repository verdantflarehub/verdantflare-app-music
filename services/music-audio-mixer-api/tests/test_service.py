import json
import os
import shutil
import subprocess
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

from verdantflare_mixer import service as mixer_module
from verdantflare_mixer.service import InvalidVocalArrangement, MixerService


class MixerServiceTest(unittest.TestCase):
    def test_rejects_misaligned_additional_vocal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(MixerService, "_prepare_audio"),
                patch.object(MixerService, "_process_vocal"),
                patch.object(MixerService, "_duration", side_effect=[10.0, 10.0, 9.5]),
            ):
                with self.assertRaisesRegex(InvalidVocalArrangement, "vocal part 1 duration"):
                    MixerService().master(
                        instrumental=root / "instrumental.wav",
                        vocal=root / "lead.wav",
                        additional_vocals=[root / "second.wav"],
                        vocal_mode="duet",
                        lrc_text="[00:00.000]test",
                        bpm=120,
                        work_directory=root,
                    )

    def test_legacy_two_track_filter_is_unchanged(self) -> None:
        graph = MixerService._mix_filter(None)
        self.assertIn("amix=inputs=2", graph)
        self.assertNotIn("[2:a]", graph)

    def test_backing_vocal_is_added_after_lead_sidechain(self) -> None:
        graph = MixerService._mix_filter(-4.0)
        self.assertIn("[0:a][1:a]sidechaincompress", graph)
        self.assertIn("[2:a]volume=-4.0dB[backing]", graph)
        self.assertIn("[ducked][1:a][backing]amix=inputs=3", graph)

    def test_master_includes_backing_track_without_changing_delivery_files(self) -> None:
        commands = []

        def fake_run(arguments, *, capture=False):
            commands.append(arguments)
            Path(arguments[-1]).touch()

        def touch_output(source, destination, *args):
            destination.touch()

        measurements = {
            "input_i": "-15",
            "input_tp": "-2",
            "input_lra": "5",
            "input_thresh": "-25",
            "target_offset": "0",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(MixerService, "_prepare_audio", side_effect=touch_output),
                patch.object(MixerService, "_process_vocal", side_effect=touch_output),
                patch.object(mixer_module, "_run", side_effect=fake_run),
                patch.object(mixer_module, "_loudness_measurement", return_value=measurements),
                patch.object(MixerService, "_duration", return_value=10.0),
            ):
                archive = MixerService().master(
                    instrumental=root / "instrumental.wav",
                    vocal=root / "vocal.wav",
                    backing_vocal=root / "backing.wav",
                    backing_gain_db=-4.0,
                    lrc_text="[00:00.00]line",
                    bpm=120,
                    work_directory=root,
                )
            self.assertIn("[2:a]volume=-4.0dB[backing]", " ".join(commands[0]))
            with zipfile.ZipFile(archive) as result:
                manifest = json.loads(result.read("manifest.json"))
                self.assertTrue(manifest["backing_vocal"])
                self.assertEqual(manifest["backing_gain_db"], -4.0)
                self.assertEqual(
                    set(result.namelist()),
                    {"Final_Song_Master.wav", "Final_Song.mp3", "Final_Song.lrc", "manifest.json"},
                )

    def test_real_ffmpeg_duet_and_choir_master(self) -> None:
        ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")

        class FfmpegOnlyMixer(MixerService):
            @staticmethod
            def _prepare_audio(source, destination):
                subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                     "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(destination)],
                    check=True,
                )

            @staticmethod
            def _process_vocal(source, destination, bpm):
                shutil.copyfile(source, destination)

            @staticmethod
            def _duration(path):
                with wave.open(str(path), "rb") as audio:
                    return audio.getnframes() / audio.getframerate()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instrumental = root / "instrumental.wav"
            vocals = [root / f"voice-{number}.wav" for number in range(3)]
            sources = [
                "anullsrc=channel_layout=stereo:sample_rate=48000:duration=3",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "sine=frequency=660:sample_rate=48000:duration=3",
                "sine=frequency=880:sample_rate=48000:duration=3",
            ]
            for source, path in zip(sources, [instrumental, *vocals]):
                subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
                     "-c:a", "pcm_s16le", str(path)],
                    check=True,
                )

            original_path = os.environ.get("PATH", "")
            with patch.dict(os.environ, {"PATH": f"{Path(ffmpeg).parent}{os.pathsep}{original_path}"}):
                for mode, extras in (("duet", vocals[1:2]), ("choir", vocals[1:3])):
                    with self.subTest(mode=mode):
                        work = root / mode
                        work.mkdir()
                        archive = FfmpegOnlyMixer().master(
                            instrumental=instrumental,
                            vocal=vocals[0],
                            additional_vocals=extras,
                            vocal_mode=mode,
                            lrc_text="[00:00.000]test\n",
                            bpm=120,
                            work_directory=work,
                        )
                        with zipfile.ZipFile(archive) as result:
                            manifest = json.loads(result.read("manifest.json"))
                            self.assertEqual(manifest["vocal_mode"], mode)
                            self.assertEqual(manifest["additional_vocal_count"], len(extras))
                            self.assertGreater(len(result.read("Final_Song.mp3")), 1000)
                            decoded = subprocess.run(
                                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                                 "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1"],
                                input=result.read("Final_Song_Master.wav"),
                                capture_output=True,
                                check=True,
                            )
                            self.assertEqual(len(decoded.stdout), 3 * 48000 * 2 * 2)


if __name__ == "__main__":
    unittest.main()
