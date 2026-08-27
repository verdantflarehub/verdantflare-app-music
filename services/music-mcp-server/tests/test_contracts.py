import unittest

from verdantflare_music_mcp.contracts import invocation, require_asset_id


class ContractTest(unittest.TestCase):
    def test_rejects_empty_asset_id(self) -> None:
        with self.assertRaises(ValueError):
            require_asset_id("  ")

    def test_invocation_keeps_only_asset_handles(self) -> None:
        result = invocation(
            "stems.separate",
            "stems",
            "/v1/audio/stem-separations",
            {},
            {"audio": "asset-123"},
            [("instrumental.wav", "audio/wav")],
        )
        self.assertEqual(result.asset_inputs, {"audio": "asset-123"})
        self.assertTrue(result.station_resolves_assets)


if __name__ == "__main__":
    unittest.main()
