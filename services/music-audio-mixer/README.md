# Music Audio Mixer API（草案）

`POST /v1/audio/masters` 接收 `instrumental`、`vocal`、`lyrics_lrc` 和 `bpm` multipart 字段；可选 `backing_vocal`（经独立试听确认不含明显原主唱的完整时轴和声轨）和 `backing_gain_db`（-24 至 +6 dB，默认 -6 dB）。不传可选字段时沿用双轨混音。返回包含以下文件的 ZIP：

- `Final_Song_Master.wav`：24-bit/48 kHz，目标 -14 LUFS、-1 dBTP
- `Final_Song.mp3`：320 kbps
- `Final_Song.lrc`：校验后原样保存的 UTF-8 LRC
- `manifest.json`

Pedalboard 负责主唱 EQ、压缩、BPM 同步延迟和混响；FFmpeg 以主唱侧链处理伴奏，再加入经音量调整的独立和声，最后做双遍响度标准化及编码。和声轨只校验与伴奏的时长差不超过 0.25 秒；来源、原主唱泄漏、相位与听感必须在上游人工审核。服务不生成和声、不把湿人声当成和声，也不自动对齐歌词，LRC 必须由调用方提供且时间戳单调。

对唱/合唱可重复上传 `additional_vocals`，同时提供 `vocal_mode=duet`（1 条追加人声）或 `vocal_mode=choir`（2 至 7 条追加人声）。可重复提供对应的 `additional_vocal_gains_db`，缺省每条 -3 dB；`solo` 保持旧双轨契约。每条人声必须有完整时间轴，未演唱处为静音，长度与伴奏相差不超过 0.25 秒。多人声部先组成总线，再共同触发伴奏侧链；服务不把一条声轨自动变成多个歌手，不自动生成或对齐重叠歌词。

当前 DSP 参数和输出契约为草案。必须使用最终伴奏、人声和 LRC 在目标环境试听并测量后才能定稿。

Pedalboard 0.9.17 使用 GPL-3.0。镜像对外分发前必须按 [`NOTICE.upstream.md`](NOTICE.upstream.md) 完成许可证审查和义务确认。
