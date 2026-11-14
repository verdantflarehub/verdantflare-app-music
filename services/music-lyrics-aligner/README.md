# Music Lyrics Aligner API

`POST /v1/lyrics/alignments` uses Stable Whisper 2.19.1 and the multilingual Whisper `small` model to force-align approved Chinese lyric lines to an actual dry-vocal WAV. It returns `Aligned_Lyrics.lrc` as UTF-8 text.

Inputs are multipart fields `audio`, `lyrics`, and `language=zh`. Lyrics must contain one output line per non-empty input line and must not contain existing LRC timestamps or Music3 section labels. The output text always comes from the submitted lyrics; the model only supplies timestamps.

Alignment fails as a whole when any line is missing, has no positive duration, produces a non-increasing millisecond timestamp, or starts after the decoded audio duration. The service does not transcribe replacement lyrics, scale an existing LRC timeline, or provide a fallback aligner.

The model cache is mounted at `/models/whisper`, separate from the image. A CUDA GPU is required. `GET /health` returns success only after the configured model has loaded.

```bash
VOCAL_FILE=/path/to/vocal.wav \
LYRICS_FILE=/path/to/lyrics.txt \
  /usr/local/bin/smoke-test.sh
```
