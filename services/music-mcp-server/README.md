# Music MCP Server

Music MCP Server v0.9.0 是音乐制作服务的可执行 MCP 边界。Streamable HTTP 入口为 `POST /mcp`；工具在收费或长任务前预检技术能力，导入受信任 S3/CDN 上的客户音频，调用集群内的 Music3、UVR5、RVC、歌词对齐、Mixer 及可选 ACE-Step 1.5 重绘 API，并把结果持久化为项目范围的 Artifact。

## 工具

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| `workflow.preflight` | 工作流、人声来源、可选人声模型 ID、是否要求局部重绘 | 服务、精确模型和编辑能力的就绪/阻断清单 |
| `asset.import` | `project_id`、S3 对象 URL、文件名、SHA-256 | 项目范围的源音频 Artifact |
| `music.generate` | `project_id`、歌词、音乐描述、候选编号、seed、最大生成秒数 | 自然结束的 Music3 候选 WAV |
| `duet.plan` | 项目 ID、逐句 `section`/`voice`/`text`/`bars`、BPM、风格描述、候选编号 | 带统一时间轴的歌词计划 JSON |
| `duet.instrumental` | 计划 Artifact、seed；可选恢复回执 ID | ACE-Step base 任务回执与伴奏 WAV |
| `duet.vocal` | 计划与伴奏 Artifact、`female` 或 `male`、seed；可选恢复回执 ID | 任务回执、原始生成轨及按角色时间窗静音的全曲人声轨 |
| `duet.preview` | 计划、伴奏、男女处理后人声 Artifact | 三轨试听预混 WAV |
| `music.redraw` | 源歌曲 Artifact、起止秒数、局部说明、歌词、修订号、seed、交叉淡化、保留模式、编辑强度 | ACE-Step repaint 后保持区间外帧不变的完整歌曲 WAV |
| `stems.separate` | `project_id`、音频 Artifact ID | 伴奏与原始干声 WAV；新版保存诊断层，配置模型后再输出待审核和声 |
| `voice.prepare` | `project_id`、1–20 个有序且唯一的录音 Artifact ID | 逐来源训练 WAV、合并训练 WAV、分段 ZIP、分析报告 |
| `voice.train` | `project_id`、录音 Artifact ID、模型 ID、epochs、batch size | RVC `.pth`、`.index`、验证 WAV |
| `voice.convert` | `project_id`、干声 Artifact ID、模型 ID、变调半音数、F0 方法、检索率、滤波半径、响度包络混合率、清辅音保护值 | 克隆干声 WAV |
| `lyrics.align` | `project_id`、人声 Artifact ID、已批准逐行歌词、语言 | 强制对齐的 UTF-8 LRC |
| `mix.master` | `project_id`、伴奏与主唱 Artifact ID、LRC 文本、BPM；可选独立和声及多声部 Artifact ID | 母带 WAV、MP3、LRC |

工具不接受宿主机路径或媒体 Base64，也不返回内部服务 URL。`mix.master` 的可选参数为 `backing_vocal_asset_id` 和 `backing_gain_db`（-24 至 +6 dB，默认 -6 dB）；和声资产须与歌曲同属一个项目且已独立试听，原湿人声或混响残留不能直接充当和声。旧部署仍只有双轨签名，使用前须读取实际工具清单；此代码变更不代表远端已部署。`asset.import` 只读取 `MUSIC_ASSET_IMPORT_ORIGINS` 明确允许的 HTTPS origin，禁用跳转，拒绝 URL 内嵌凭据和 fragment，按文件扩展名限制为音频，并要求调用方提供 SHA-256。对象以最多 1 GiB 的流式内容写入临时目录，大小和 SHA-256 验证成功后才原子登记；签名 URL 不得写入项目记录。每个输出包含不可预测的 Artifact ID、文件名、媒体类型、大小、SHA-256 和下载路径。配置 `MUSIC_MCP_PUBLIC_BASE_URL` 后还会返回可直接读取的绝对资源链接。

多声部参数为 `vocal_mode=duet|choir`、`additional_vocal_asset_ids` 与可选的 `additional_vocal_gains_db`；对唱恰好追加一条，合唱追加两至七条已对齐的独立人声。`solo` 是默认值并保持旧调用。男声、女声或声部身份由各自的源人声及模型决定，工具不会从一条混合干声自动派生多位歌手。当前歌词对齐器只处理单条人声，重叠歌词需要另行取得真实、已批准的时间轴。

纯歌词对唱走 `workflow.preflight(workflow="duet_generate")`，再依次调用四个 `duet.*` 工具。每句默认两小节，必须包含女声独唱、男声独唱和同唱句；客户端可先把纯歌词整理成逐句角色计划供创作审核。每版启动一次 `text2music` 和两次 `lego`，需 ACE-Step 1.5 base 或 xl-base 模型；turbo/sft 不支持此路径。推理提交后保存 `duet.task` 回执；若查询中断，使用相同参数和 `resume_task_asset_id` 恢复。时长校验失败的伴奏及人声校验失败的原始轨仍保留 Artifact 以便诊断。预混只是试听候选，不能替代逐轨听审；时间窗、时长和内容不同不证明男女音色、歌词、纯人声隔离或曲风达标。实际部署以工具清单和预检结果为准；未配置后端的部署预检会返回 `blocked`。

UVR5 可选分离模型返回的 `backing_vocals_unreviewed.wav` 只是待审核资产。先试听是否含原主唱、错词与相位问题，再决定是否作为混音的独立和声输入；MCP 持久化不代表音质批准。

`workflow.preflight` 不替代人声授权、模型许可或艺术批准。完整歌曲和翻唱必须先选择 `voice_source`：`generated_voice`、`approved_model` 或 `authorized_recordings`；选择已批准模型时必须提供并验证实际 `voice_model_id`，选择授权录音时则先验证训练链路，训练后再用返回的模型 ID 复检。`music.redraw` 只在 `MUSIC_EDITOR_URL` 指向 ACE-Step 1.5 API 时可用；它调用真实 `repaint`，完成后将编辑区间重采样至源格式并与源文件的区间外原始帧装配。当前 Music3 接口没有参考音频、续写或区间编辑能力，因此 MCP 不会以整曲重生成冒充局部重绘。

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
| `MUSIC_EDITOR_URL` | 未设置 | ACE-Step 1.5 API 根地址；未设置时预检报告不可用 |
| `MUSIC_DUET_MODEL` | `acestep-v15-base` | 纯歌词对唱使用的 ACE-Step base / xl-base 精确模型名 |
| `MUSIC_EDITOR_BACKEND` | `ace_step_v1_5` | 重绘适配器类型；当前只接受该值 |
| `MUSIC_EDITOR_BEARER_TOKEN` | 未设置 | ACE-Step API 启用鉴权时使用的可选 Bearer Token |
| `MUSIC3_URL` | `http://music-minimax-music3-api:8000` | Music3 API |
| `UVR5_URL` | `http://music-uvr5-api:8000` | UVR5 API |
| `RVC_URL` | `http://music-rvc-api:8000` | RVC API |
| `LYRICS_ALIGNER_URL` | `http://music-lyrics-aligner-api:8000` | 已知歌词强制对齐 API |
| `MIXER_URL` | `http://music-audio-mixer-api:8000` | Mixer API |

Token 只能通过运行环境注入，不得写入镜像、清单或仓库。

## 成都集群验证

镜像 `music-mcp-server-v0.9.0` 由 `release` 流水线发布后，部署声明式清单。MCP 保持 ClusterIP，通过端口转发验证：

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
