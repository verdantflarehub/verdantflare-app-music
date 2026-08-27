import unittest

from verdantflare_mixer.lrc import InvalidLRC, validate_lrc


class LRCTest(unittest.TestCase):
    def test_accepts_monotonic_utf8_lrc(self) -> None:
        content = "[00:01.000]第一句\n[00:02.50]第二句\n".encode()
        self.assertEqual(validate_lrc(content), content.decode())

    def test_rejects_out_of_order_timestamps(self) -> None:
        with self.assertRaisesRegex(InvalidLRC, "monotonic"):
            validate_lrc(b"[00:02.000]two\n[00:01.000]one\n")

    def test_rejects_metadata_without_timestamps(self) -> None:
        with self.assertRaises(InvalidLRC):
            validate_lrc(b"[ar:artist]\n")


if __name__ == "__main__":
    unittest.main()
