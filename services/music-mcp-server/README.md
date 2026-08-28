# Music MCP Server

Music MCP Server v0.2.0 是音乐制作服务的可执行 MCP 边界。Streamable HTTP 入口为 `POST /mcp`；工具直接调用集群内的 Music3、UVR5、RVC 和 Mixer API，并把结果持久化为项目范围的 Artifact。

## 工具

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `music.generate` | `project_id`、歌词、音乐描述、候选编号、seed、精确秒数 | 原始 Music3 WAV、精确时长 WAV |
| `stems.separate` | `project_id`、音频 Artifact ID | 伴奏 WAV、原始干声 WAV |
| `voice.train` | `project_id`、录音 Artifact ID、模型 ID、epochs、batch size | RVC `.pth`、`.index`、验证 WAV |
| `voice.convert` | `project_id`、干声 Artifact ID、模型 ID、变调半音数 | 克隆干声 WAV |
| `mix.master` | `project_id`、伴奏与人声 Artifact ID、LRC 文本、BPM | 母带 WAV、MP3、LRC |

工具不接受宿主机路径，也不返回内部服务 URL。每个输出包含不可预测的 Artifact ID、文件名、媒体类型、大小、SHA-256 和下载路径。配置 `MUSIC_MCP_PUBLIC_BASE_URL` 后还会返回可直接读取的绝对资源链接。

Artifact 只允许在创建它的 `project_id` 下作为后续工具输入。内容接口为：

```text
GET /artifacts/{artifact_id}/content
```

## 精确时长

`music.generate` 按 Music3 的 25 token/s 向上计算 `max_new_tokens`。例如 90 秒请求使用 2250 tokens。服务同时保留 Music3 原始 WAV，并按 WAV 采样帧裁剪精确版本；若模型输出不足请求时长，调用失败，不用静音补齐。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MUSIC_ARTIFACT_ROOT` | `/data/projects` | Artifact 元数据和内容目录 |
| `MUSIC_MCP_PUBLIC_BASE_URL` | 未设置 | Artifact 绝对下载地址前缀 |
| `MUSIC_MCP_BEARER_TOKEN` | 未设置 | 可选 Bearer 鉴权；启用后保护 MCP 和 Artifact，`/health` 除外 |
| `MUSIC3_URL` | `http://music-minimax-music3-api:8000` | Music3 API |
| `UVR5_URL` | `http://music-uvr5-api:8000` | UVR5 API |
| `RVC_URL` | `http://music-rvc-api:8000` | RVC API |
| `MIXER_URL` | `http://music-audio-mixer-api:8000` | Mixer API |

Token 只能通过运行环境注入，不得写入镜像、清单或仓库。

## 成都集群验证

镜像 `music-mcp-server-v0.2.0` 由 `release` 流水线发布后，部署声明式清单。MCP 保持 ClusterIP，通过端口转发验证：

```bash
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-mcp-server 8005:8000

codex mcp add verdantflare-music --url http://127.0.0.1:8005/mcp
```

注册后新开 Codex 会话加载工具。可先用 `GET http://127.0.0.1:8005/health` 检查服务；未配置 Bearer 时不需要凭据。项目资产保存在 `music-projects` 的 `hostpath` PVC 中，删除 PVC 前必须确认保留策略。
