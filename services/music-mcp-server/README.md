# Music MCP Server

Music MCP Server v0.6.0 是音乐制作服务的可执行 MCP 边界。Streamable HTTP 入口为 `POST /mcp`；工具导入受信任 S3/CDN 上的客户音频，调用集群内的 Music3、UVR5、RVC、歌词对齐和 Mixer API，并把结果持久化为项目范围的 Artifact。

## 工具

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `asset.import` | `project_id`、S3 对象 URL、文件名、SHA-256 | 项目范围的源音频 Artifact |
| `music.generate` | `project_id`、歌词、音乐描述、候选编号、seed、最大生成秒数 | 自然结束的 Music3 候选 WAV |
| `stems.separate` | `project_id`、音频 Artifact ID | 伴奏 WAV、原始干声 WAV |
| `voice.prepare` | `project_id`、1–20 个有序且唯一的录音 Artifact ID | 逐来源训练 WAV、合并训练 WAV、分段 ZIP、分析报告 |
| `voice.train` | `project_id`、录音 Artifact ID、模型 ID、epochs、batch size | RVC `.pth`、`.index`、验证 WAV |
| `voice.convert` | `project_id`、干声 Artifact ID、模型 ID、变调半音数、F0 方法、检索率、滤波半径、响度包络混合率、清辅音保护值 | 克隆干声 WAV |
| `lyrics.align` | `project_id`、人声 Artifact ID、已批准逐行歌词、语言 | 强制对齐的 UTF-8 LRC |
| `mix.master` | `project_id`、伴奏与人声 Artifact ID、LRC 文本、BPM | 母带 WAV、MP3、LRC |

工具不接受宿主机路径或媒体 Base64，也不返回内部服务 URL。`asset.import` 只读取 `MUSIC_ASSET_IMPORT_ORIGINS` 明确允许的 HTTPS origin，禁用跳转，拒绝 URL 内嵌凭据和 fragment，按文件扩展名限制为音频，并要求调用方提供 SHA-256。对象以最多 1 GiB 的流式内容写入临时目录，大小和 SHA-256 验证成功后才原子登记；签名 URL 不得写入项目记录。每个输出包含不可预测的 Artifact ID、文件名、媒体类型、大小、SHA-256 和下载路径。配置 `MUSIC_MCP_PUBLIC_BASE_URL` 后还会返回可直接读取的绝对资源链接。

Artifact 只允许在创建它的 `project_id` 下作为后续工具输入。内容接口为：

```text
GET /artifacts/{artifact_id}/content
```

## 自然时长

`music.generate` 的 `max_duration_seconds` 是生成上限，不是目标时长。服务按 Music3 的 25 token/s 向上计算 `max_new_tokens`，例如 90 秒上限使用 2250 tokens。模型发出音频结束标记时，服务验证并原样保存自然结束的 PCM WAV；不要求音频填满上限，不裁切，不补静音，也不额外生成 `.generated.wav`。

## 配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MUSIC_ARTIFACT_ROOT` | `/data/projects` | Artifact 元数据和内容目录 |
| `MUSIC_ASSET_IMPORT_ORIGINS` | 未设置 | 允许导入的逗号分隔 HTTPS S3/CDN origin；未设置时禁用导入 |
| `MUSIC_MCP_PUBLIC_BASE_URL` | 未设置 | Artifact 绝对下载地址前缀 |
| `MUSIC_MCP_BEARER_TOKEN` | 未设置 | 可选 Bearer 鉴权；启用后保护 MCP 和 Artifact，`/health` 除外 |
| `MUSIC_MCP_ALLOWED_HOSTS` | loopback host | MCP DNS rebinding 防护允许的逗号分隔 Host；公网部署必须显式配置 |
| `MUSIC_MCP_ALLOWED_ORIGINS` | loopback origin | MCP DNS rebinding 防护允许的逗号分隔 Origin；无 Origin 的非浏览器客户端不受影响 |
| `MUSIC3_URL` | `http://music-minimax-music3-api:8000` | Music3 API |
| `UVR5_URL` | `http://music-uvr5-api:8000` | UVR5 API |
| `RVC_URL` | `http://music-rvc-api:8000` | RVC API |
| `LYRICS_ALIGNER_URL` | `http://music-lyrics-aligner-api:8000` | 已知歌词强制对齐 API |
| `MIXER_URL` | `http://music-audio-mixer-api:8000` | Mixer API |

Token 只能通过运行环境注入，不得写入镜像、清单或仓库。

## 成都集群验证

镜像 `music-mcp-server-v0.6.0` 由 `release` 流水线发布后，部署声明式清单。MCP 保持 ClusterIP，通过端口转发验证：

```bash
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-mcp-server 8005:8000

codex mcp add verdantflare-music --url http://127.0.0.1:8005/mcp
```

注册后新开 Codex 会话加载工具。可先用 `GET http://127.0.0.1:8005/health` 检查服务；未配置 Bearer 时不需要凭据。项目资产保存在 `music-projects` 的 `hostpath` PVC 中，删除 PVC 前必须确认保留策略。

### 公网 MCP

成都验证环境通过 BCC `IngressRoute` 将公网 `POST https://mcp.cn-chengdu.bc-cloud.com/music` 重写到服务的 `/mcp`，并将 `/music/artifacts/` 转发到受保护的 Artifact 下载接口。公网不路由 `/health`。

部署前必须在 `verdantflare-music` namespace 创建 `music-mcp-auth` Secret，其中只包含 `MUSIC_MCP_BEARER_TOKEN`；Token 使用安全随机值生成，不得写入清单、Git 或终端记录。Deployment 同时配置公网 Host 与 Origin allowlist，保留 MCP SDK 的 DNS rebinding 防护。

客户端进程从安全环境注入同一个 Token，然后注册公网 MCP：

```bash
codex mcp add verdantflare-music \
  --url https://mcp.cn-chengdu.bc-cloud.com/music \
  --bearer-token-env-var MUSIC_MCP_BEARER_TOKEN
```

注册后新开 Codex 会话。未带 Token 的 MCP 与 Artifact 请求必须返回 `401`，允许的公网 Host 必须完成 MCP 初始化，其他 Host 必须由 transport security 拒绝。
