# 歌词强制对齐服务设计

> 状态：已批准。批准时间：2026-08-29。

## 1. 目标与边界

新增独立的 `music-lyrics-aligner-api`，将已批准的逐行歌词强制对齐到项目内的实际演唱干声，输出 UTF-8 LRC。`music-mcp-server` 新增 `lyrics.align`，只接收项目范围的人声 Artifact ID 和纯歌词文本，并将结果登记为 `Aligned_Lyrics.lrc` Artifact。

本服务不负责自由转写、翻译、改写歌词、混音或母带。不用原曲 LRC 时间轴作 fallback，不为通过校验而线性缩放时间戳，不伪造未对齐成功的歌词行。

## 2. 成熟方案评估

| 方案 | 适合点 | 代价与风险 | 结论 |
| --- | --- | --- | --- |
| Stable Whisper (`stable-ts`) | 官方接口支持 `align(audio, text)`，直接对齐已知正确文本；`original_split=True` 可保留换行分段；中文按非空格语言分词 | Whisper 对歌声的注意力对齐仍需真实音频验收 | **采用** |
| WhisperX | 成熟的 ASR、VAD 和词级音素对齐 | 标准流程是先转写再对齐；中文需额外语言特定 wav2vec2 模型，与“已知批准歌词”的边界不如 A 直接 | 不采用 |
| Montreal Forced Aligner | 专业的语音强制对齐系统 | 需中文词典、声学模型、Kaldi/Pynini/OpenFST 运行时；部署更重，且仍需单独评估歌声 | 不采用 |

锁定 `stable-ts==2.19.1`（MIT）及其约束的 `openai-whisper==20250625`。首个版本使用 Whisper `small` 多语言模型和 `zh` 语言标识；不在 API 中暴露可任意选模型的配置。

## 3. 内部 HTTP 契约

### `POST /v1/lyrics/alignments`

`multipart/form-data` 输入：

- `audio`：实际演唱干声 WAV，最大 500 MiB。
- `lyrics`：UTF-8 纯歌词文件，最大 1 MiB。非空行即 LRC 行，不接收时间戳或 Music3 段落标签。
- `language`：首版固定为 `zh`，默认 `zh`。

成功返回 `text/plain; charset=utf-8`，文件名 `Aligned_Lyrics.lrc`，每行格式为 `[mm:ss.mmm]<原歌词行>`。输出文字来自输入，模型只决定时间戳。

失败语义：

- `413`：音频或歌词超限。
- `422`：空输入、非 UTF-8、已含 LRC 时间戳、包含段落标签或无法解码的音频。
- `503`：CUDA 或 Whisper 模型不可用。
- `500`：对齐失败，包括行数不一致、任一行无时长、毫秒级时间戳不严格递增或超过音频时长。

### `GET /health`

只有 CUDA 可见且 Whisper `small` 模型已加载时返回 `200`；响应包含锁定模型名、语言和对齐后端版本，不包含内部路径。

## 4. MCP 契约

新增：

```text
lyrics.align(
  project_id: string,
  vocal_asset_id: string,
  lyrics: string,
  language: "zh" = "zh"
) -> Aligned_Lyrics.lrc Artifact
```

MCP Server 必须先用 `project_id` 校验人声 Artifact 的项目归属，再调用内部服务。对齐文本最大 1 MiB。输出操作名为 `lyrics.align`，不返回内部服务 URL、模型路径或媒体 Base64。

`mix.master` 保持单步工具，不在服务端暗中触发对齐。Skill 在对齐 Artifact 通过自动检查后读取 LRC 文本并显式调用 `mix.master`。

## 5. 对齐实现与校验

1. 将上传音频流式写入受限临时目录，用 FFmpeg 解码为 16 kHz 单声道 PCM。
2. 解析歌词：只对 CRLF 做归一化，去除空行及每行首尾空白，保留其余文字和顺序。
3. 调用 `model.align(audio, "\n".join(lines), language="zh", original_split=True, failure_threshold=0.0)`。对齐调用使用进程内互斥锁，避免同一 GPU 并发推理。
4. 使用每个对齐 segment 的首个词开始时间作为该行 LRC 时间戳，输出文字仍使用对应输入行。
5. 对输出执行全量不变式校验：行数相等，歌词逐行完全相等，时间戳精确到毫秒后严格递增，首行不早于 0，最后一行不晚于音频结束，每个 segment 时长为正。

首版不增加自动文本修复、相似度猜测或第二对齐引擎 fallback。

## 6. 部署

- 镜像：`music-lyrics-aligner-api-v0.1.0`，单独 release workflow，只在 `release` 分支且路径命中时发布。
- 基础镜像：锁定版本的 PyTorch CUDA runtime tag，不使用 digest 或 SHA 镜像标签。
- 调度：固定到 `10.241.109.7`，由 `hami-scheduler` 申请 1 张完整 RTX 4090，要求 `nvidia.com/gpu.sharing-strategy=none`，不配置显存或核心切分。
- 资源：request `4 CPU / 16 GiB / 1 GPU`，limit `8 CPU / 32 GiB / 1 GPU`；一个 replica，`Recreate` 策略。
- 模型：新增 `lyrics-aligner-models` `hostpath` PVC，挂载到 `/models/whisper`；模型与镜像分离。不使用 JuiceFS 或 S3 PVC。
- 临时数据：`emptyDir` 挂载 `/tmp/lyrics-aligner`，限制 10 GiB，请求结束后删除。
- 网络：ClusterIP Service，只由 MCP Server 调用；验收时仅通过显式 `kubectl --context chengdu.beagle -n verdantflare-music port-forward` 访问。

部署后六个服务均固定在同一节点，常驻 GPU 请求从 4 张增加到 5 张，节点仍保留 3 张未申请 GPU。

## 7. 测试与验收

单元和契约测试：

- 纯歌词解析、LRC 格式化、严格时间戳校验及全部失败分支。
- 用模拟对齐后端验证 HTTP 上传限制和返回契约，CI 不下载模型。
- MCP `lyrics.align` 的项目隔离、上游 multipart 请求、Artifact 持久化和工具 schema。

成都真实验收：

1. 重新检查节点 Ready、完整 GPU、`hostpath` 和 `/data` 容量；确认新 Pod 只看到一个 CUDA GPU UUID。
2. 对 `vocal_dry_cloned.wav` 和 65 行已批准歌词调用 `lyrics.align`。
3. 校验 LRC 恰好 65 行、文字逐行一致、时间戳严格递增且最后一行不超过 199.740 秒。
4. 人工抽查首段、首个副歌、Bridge 和最终副歌的字幕进入时点。自动校验通过不代表对齐听感通过。
5. 对齐通过后显式调用 `mix.master`，下载 WAV、MP3、LRC，检查时长、格式、`-14 ± 0.5 LUFS`、True Peak 不高于 `-1.0 dBTP`，再停在审核点 5。

## 8. 交付范围

批准本草案后交付：新服务源码与测试、MCP 工具、独立 workflow、README 和 NOTICE、成都声明式部署和 PVC、更新后的验收文档与脚本，以及当前项目的 `Aligned_Lyrics.lrc`、最终三件产物和审核记录。
