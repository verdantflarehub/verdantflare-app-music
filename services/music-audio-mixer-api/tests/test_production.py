import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np
import soundfile as sf
from pydantic import ValidationError
from verdantflare_mixer.production import (
    RATE,
    Tune,
    Note,
    Instrument,
    Process,
    Mix,
    Track,
    Vocoder,
    instrument,
    effects,
    render_mix,
    world_transform,
    render,
    automation,
    Point,
)


class ProductionTest(unittest.TestCase):
    def test_instrument_is_repeatable_and_note_pitch_is_correct(self):
        cfg = Instrument(
            duration_seconds=0.5,
            notes=[Note(start_seconds=0.05, duration_seconds=0.3, midi=69)],
            waveform="sine",
            detune_cents=0,
        )
        a = instrument(cfg)
        b = instrument(cfg)
        np.testing.assert_array_equal(a, b)
        self.assertTrue(np.all(a[:2000] == 0))
        window = a[4000:16000, 0]
        f = np.fft.rfftfreq(len(window), 1 / RATE)
        self.assertAlmostEqual(f[np.argmax(abs(np.fft.rfft(window)))], 440, delta=4)

    def test_tuning_changes_pitch_without_changing_length(self):
        t = np.arange(RATE) / RATE
        x = np.repeat(
            (sum(0.15 / k * np.sin(2 * np.pi * 440 * k * t) for k in range(1, 10)))[
                :, None
            ],
            2,
            axis=1,
        )
        y, r = world_transform(
            x,
            Tune(
                mode="notes",
                notes=[Note(start_seconds=0, duration_seconds=1, midi=71)],
                strength=1,
                retune_ms=0,
                vibrato_preserve=0,
            ),
        )
        self.assertEqual(x.shape, y.shape)
        self.assertGreater(r["voiced_frames"], 100)
        f = np.fft.rfftfreq(24000, 1 / RATE)
        peak = f[np.argmax(abs(np.fft.rfft(y[12000:36000, 0])))]
        self.assertAlmostEqual(peak, 493.883, delta=4)
        bypass, _ = world_transform(x, Tune(strength=0))
        np.testing.assert_array_equal(x, bypass)

    def test_vocoder_requires_carrier_and_retains_duration(self):
        t = np.arange(RATE // 4) / RATE
        x = np.repeat((0.1 * np.sin(2 * np.pi * 220 * t))[:, None], 2, axis=1)
        with self.assertRaises(ValueError):
            effects(x, [Vocoder()])
        carrier = instrument(
            Instrument(
                duration_seconds=0.25,
                notes=[Note(start_seconds=0, duration_seconds=0.2, midi=60)],
            )
        )
        y = effects(x, [Vocoder(mix=1)], carrier)
        self.assertEqual(x.shape, y.shape)
        self.assertGreater(np.max(abs(y)), 0.001)
        self.assertFalse(np.allclose(x, y))

    def test_mix_offsets_pan_and_no_hidden_processing(self):
        x = np.ones((480, 2)) * 0.1
        cfg = Mix(
            duration_seconds=0.02,
            output_gain_db=0,
            tracks=[Track(offset_seconds=0.01, pan=1)],
        )
        y = render_mix([x], cfg)
        np.testing.assert_array_equal(y[:480], 0)
        np.testing.assert_array_equal(y[:, 0], 0)
        np.testing.assert_allclose(y[480:, 1], 0.1)
        with self.assertRaises(ValueError):
            render_mix([x], Mix(duration_seconds=0.005, tracks=[Track()]))

    def test_reject_invalid_controls(self):
        for cfg in [
            {"strength": float("nan")},
            {"unknown": 1},
            {"pitch_classes": [12]},
            {"mode": "notes"},
        ]:
            with self.assertRaises(ValidationError):
                Tune.model_validate(cfg)
        with self.assertRaises(ValidationError):
            Mix(duration_seconds=1, tracks=[Track(duck_from_track=1)])
        with self.assertRaises(ValueError):
            automation([Point(seconds=0.2, value=1), Point(seconds=0.1, value=0)], RATE)
        with self.assertRaises(ValidationError):
            Instrument(
                duration_seconds=0.1,
                notes=[Note(start_seconds=0, duration_seconds=0.2, midi=60)],
            )

    def test_archive_has_source_hash_config_and_unclipped_stems(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "source.wav"
            sf.write(p, np.ones((4800, 2)) * 0.1, RATE)
            raw = render("vocal.process", {"effects": [], "tail_seconds": 0}, [p])
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                report = json.loads(z.read("report.json"))
                self.assertEqual(report["duration_seconds"], 0.1)
                self.assertEqual(len(report["inputs_sha256"][0]), 64)
                np.testing.assert_array_equal(
                    sf.read(io.BytesIO(z.read("render.wav")))[0], sf.read(p)[0]
                )


class ProductionAPITest(unittest.TestCase):
    def test_schema_and_http_render_share_validation(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from verdantflare_mixer import api

        with tempfile.TemporaryDirectory() as directory, patch.object(
            api, "TEMP_ROOT", Path(directory)
        ):
            client = TestClient(api.app)
            schemas = client.get("/v1/production/capabilities").json()["schemas"]
            self.assertEqual(
                set(schemas),
                {"vocal.tune", "vocal.process", "instrument.render", "mix.render"},
            )
            config = {
                "duration_seconds": 0.2,
                "notes": [{"start_seconds": 0, "duration_seconds": 0.1, "midi": 69}],
            }
            result = client.post(
                "/v1/production/render",
                data={
                    "operation": "instrument.render",
                    "config_json": json.dumps(config),
                },
                files=[("placeholder", (None, ""))],
            )
            self.assertEqual(result.status_code, 200)
            bad = client.post(
                "/v1/production/render",
                data={
                    "operation": "instrument.render",
                    "config_json": json.dumps({**config, "plugin_path": "/tmp/evil"}),
                },
            )
            self.assertEqual(bad.status_code, 422)
