# Music Audio Mixer API（草案）

`POST /v1/audio/masters` 接收 `instrumental`、`vocal`、`lyrics_lrc` 和 `bpm` multipart 字段，返回包含以下文件的 ZIP：

- `Final_Song_Master.wav`：24-bit/48 kHz，目标 -14 LUFS、-1 dBTP
- `Final_Song.mp3`：320 kbps
- `Final_Song.lrc`：校验后原样保存的 UTF-8 LRC
- `manifest.json`

Pedalboard 负责人声 EQ、压缩、BPM 同步延迟和混响；FFmpeg 负责侧链混合、双遍响度标准化及编码。服务不生成或自动对齐歌词，LRC 必须由调用方提供且时间戳单调。

当前 DSP 参数和输出契约为草案。必须使用最终伴奏、人声和 LRC 在目标环境试听并测量后才能定稿。

Pedalboard 0.9.17 使用 GPL-3.0。镜像对外分发前必须按 [`NOTICE.upstream.md`](NOTICE.upstream.md) 完成许可证审查和义务确认。
