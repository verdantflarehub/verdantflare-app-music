# 端到端验收（草案）

`scripts/acceptance-music-workflow.sh` 是对应 `verdantflare_music.md` 输入输出的下游 HTTP API 诊断入口，用于独立定位 Music3、UVR5、RVC 和 Mixer。正式制作流程通过 Music MCP Server 的可执行 v1 工具传递项目 Artifact ID；两条路径使用相同的真实模型与音频输出，均不能替代人工音质审核。

必需输入：

- `PLAN_FILE`：已批准的 `完整词曲企划.md`
- `LYRICS_FILE`：MiniMax 段落标签歌词
- `INSTRUCTIONS_FILE`：MiniMax 音乐描述
- `USER_VOICE_FILE`：约 10 分钟真人录音
- `LRC_FILE`：已对齐的 UTF-8 LRC
- `BPM`：40 到 240
- `OUTPUT_DIR`：尚不存在的新目录

```bash
PLAN_FILE=/data/input/完整词曲企划.md \
LYRICS_FILE=/data/input/lyrics.txt \
INSTRUCTIONS_FILE=/data/input/instructions.txt \
USER_VOICE_FILE=/data/input/User_Voice_10min.mp3 \
LRC_FILE=/data/input/song.lrc \
BPM=92 \
MODEL_ID=TonyStark \
OUTPUT_DIR=/data/acceptance/music3-run-001 \
scripts/acceptance-music-workflow.sh
```

默认端口为 Music3 `8001`、UVR5 `8002`、RVC `8003`、Mixer `8004`，可通过对应 `*_URL` 环境变量覆盖。由于 voice model 不可覆盖，每次重新训练必须使用新 `MODEL_ID`。脚本成功只证明接口和格式链路通过；旋律、分轨、音色和母带质量仍需完成五个人工审核点。
