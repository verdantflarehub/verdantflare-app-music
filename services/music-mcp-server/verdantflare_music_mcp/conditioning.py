"""Bound ACE-Step text inputs before submission; never silently trim instructions."""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re

TOKENIZER_SHA256 = "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"
TOKENIZER_URL = ("https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/"
                 "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3/tokenizer.json")
CAPTION_BUDGET = 192  # upstream 256, with room for instruction and metadata
LYRICS_BUDGET = 1800  # upstream 2048, including the language header below


class BudgetExceeded(ValueError):
    """Safe structured input feedback, distinct from internal service errors."""

    def __init__(self, report):
        self.report = report
        super().__init__(
            f"conditioning token budget exceeded: caption {report['caption_tokens']}/{CAPTION_BUDGET}, "
            f"lyrics {report['lyrics_tokens']}/{LYRICS_BUDGET}; shorten text explicitly; nothing submitted"
        )


@lru_cache(maxsize=1)
def tokenizer():
    from tokenizers import Tokenizer

    path = Path(os.environ.get("MUSIC_CONDITIONING_TOKENIZER", "/app/tokenizer.json"))
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise RuntimeError("conditioning tokenizer checksum mismatch")
    value = Tokenizer.from_str(raw.decode("utf-8"))
    value.no_truncation()
    return value


def check(instructions, lyrics, vocal_language="unknown"):
    if not isinstance(vocal_language, str) or not re.fullmatch(r"(?:[a-z]{2,3}|unknown)", vocal_language):
        raise ValueError("vocal_language must be a lowercase language code or unknown")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("instructions are required")
    if not isinstance(lyrics, str):
        raise ValueError("lyrics must be text")
    if max(len(instructions), len(lyrics)) > 16000:
        raise ValueError("conditioning text exceeds character safety limit")
    # Prevent a user caption from becoming a second upstream SFT template.
    if any(marker in instructions for marker in ("# Instruction", "# Caption", "# Metas", "<|endoftext|>")):
        raise ValueError("instructions must be a plain caption without backend template markers")
    enc = tokenizer()
    caption_tokens = len(enc.encode(instructions, add_special_tokens=False).ids)
    lyric_input = f"# Languages\n{vocal_language}\n\n# Lyric\n{lyrics}<|endoftext|>"
    lyrics_tokens = len(enc.encode(lyric_input, add_special_tokens=False).ids)
    report = {"caption_tokens": caption_tokens, "caption_budget": CAPTION_BUDGET,
              "lyrics_tokens": lyrics_tokens, "lyrics_budget": LYRICS_BUDGET,
              "vocal_language": vocal_language, "tokenizer_sha256": TOKENIZER_SHA256,
              "truncated": False, "valid": True}
    if caption_tokens > CAPTION_BUDGET or lyrics_tokens > LYRICS_BUDGET:
        report.update(valid=False, code="conditioning_budget_exceeded",
                      action="Shorten the supplied text explicitly before local editing; no inference was submitted.")
        raise BudgetExceeded(report)
    return report


def inspect_budget(instructions, lyrics, vocal_language="unknown"):
    """Public preflight result; expected over-budget input is not an opaque tool failure."""
    try:
        return check(instructions, lyrics, vocal_language)
    except BudgetExceeded as error:
        return error.report


if __name__ == "__main__":
    # Build-time only. Runtime is offline and fails closed if the file is absent.
    import sys
    from urllib.request import urlopen

    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m verdantflare_music_mcp.conditioning TOKENIZER_PATH")
    with urlopen(TOKENIZER_URL, timeout=120) as response:
        raw = response.read(16 * 1024 * 1024)
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise SystemExit("conditioning tokenizer download checksum mismatch")
    Path(sys.argv[1]).write_bytes(raw)
