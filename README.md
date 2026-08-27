# VerdantFlare App Music

VerdantFlare App Music 是部署在 VerdantFlare Station 上的 AI 音乐制作应用套件。它通过版本化的 MCP 工具和内部 HTTP API，提供音乐生成、音轨分离、人声模型训练、音色转换、混音与母带处理能力。

当前已打通 HTTP-first 最小端到端实现：MiniMax Music 3 生成、UVR5 分轨与去混响、RVC v2 训练与转换、混音母带、MCP 结构化调用描述及业务交付物验收脚本。所有 GPU 推理质量、DSP 参数和 Station 资源句柄集成仍是待实机验收的草案。

## 项目职责

本仓库负责在 VerdantFlare Station 上运行的以下服务：

| 服务                       | 职责                                                                |
| -------------------------- | ------------------------------------------------------------------- |
| `music-mcp-server`         | 在 Station MCP 边界暴露音乐制作工具，校验请求并向内部服务提交任务。 |
| `music-minimax-music3-api` | 提供 MiniMax Music 3 候选歌曲生成 API。                              |
| `music-uvr5-api`           | 提供音轨分离、去混响、模型加载和任务状态 API。                      |
| `music-rvc-api`            | 提供人声模型训练、模型管理和音色转换 API。                          |
| `music-audio-mixer-api`    | 提供混音、母带处理、响度标准化和交付格式编码 API。                  |

本仓库不负责 Agent Skill 和 Studio 用户界面：

- `verdantflare-music` Skill 位于 `verdantflare-skills/skills/verdantflare-music/`，负责将用户意图转换为制作计划、补全参数、应用审核点并选择 MCP 工具。
- VerdantFlare Studio 负责展示计划、操作确认、任务、资产、审核和交付状态。
- Station Runtime 负责 GPU 调度、任务 Attempt、资源租约、项目范围存储和 Artifact 登记。

完整执行边界如下：

```text
用户
  -> verdantflare-music Skill
  -> Studio MCP Bridge
  -> music-mcp-server
  -> generator / UVR5 / RVC / mixer API
  -> Station 任务和项目资产
```

Skill 负责制定计划，MCP Server 负责暴露受控工具，API 服务负责执行实际工作。制作流程编排不得在 Skill 和 MCP Server 中重复实现。

## 制作流程

目标工作流包含三个业务阶段，并在阶段之间设置明确的人工审核点：

1. 创建并审批结构化词曲企划，然后生成候选 Demo。
2. 分离选定 Demo 的音轨，并训练或选择已批准的人声模型。
3. 转换人声音色，完成混音和母带处理，然后审批最终交付包。

MCP 能力与服务边界一一对应：

```text
music.generate
stems.separate
voice.train
voice.convert
mix.master
```

这些工具当前标记为 `v1-draft`。MCP 返回内部 HTTP 调用描述，Station Bridge 负责将资产 ID 解析为 multipart 文件流并登记结果；Station 资源接口完成集成评审前，Schema 不定稿。

## 仓库结构

源码目录按可独立部署的服务组织：

```text
.
├── .github/
│   └── workflows/
├── deploy/
│   └── chengdu.beagle/
│       └── verdantflare-music/
├── README.md
├── services/
│   ├── music-mcp-server/
│   ├── music-minimax-music3-api/
│   ├── music-uvr5-api/
│   ├── music-rvc-api/
│   └── music-audio-mixer-api/
├── docs/
│   └── acceptance.md
└── scripts/
    └── acceptance-music-workflow.sh
```

自研 Python 服务使用相同的内部目录结构：

```text
services/<service>/
├── Dockerfile
├── requirements.txt
├── <python_package>/
└── tests/
```

服务依赖、镜像构建文件和测试保留在各自服务目录内。直接采用成熟上游运行时的 Music3 服务不重复包装 Python 应用；UVR5、RVC 和 Mixer 只包装 VerdantFlare 所需的受控 HTTP 契约。只有在实际存在需要消除的跨服务重复代码时，才引入共享 Python 包。

`.github/workflows/` 按一个应用一个 YAML 负责测试、构建五个版本化服务镜像并推送镜像仓库，只在 `release` 分支触发。`deploy/chengdu.beagle/verdantflare-music/` 保存目标验证集群的声明式 Kubernetes 配置；Station Runtime 资源接口仍需单独完成集成评审。

UVR5 和 RVC 是由本仓库构建和发布的一等服务。项目可以使用锁定版本的成熟上游实现作为算法依赖，但上游公开的 WebUI、CLI 或容器接口不是 VerdantFlare 的服务契约。

## 镜像命名

五个服务镜像统一发布到 `verdantflare-app` 镜像仓库，以服务名和语义化版本组成 tag：

| 服务                       | 镜像 tag 格式                                 |
| -------------------------- | --------------------------------------------- |
| `music-mcp-server`         | `music-mcp-server-vx.x.x`                     |
| `music-minimax-music3-api` | `music-minimax-music3-api-vx.x.x`             |
| `music-uvr5-api`           | `music-uvr5-api-vx.x.x`                       |
| `music-rvc-api`            | `music-rvc-api-vx.x.x`                        |
| `music-audio-mixer-api`    | `music-audio-mixer-api-vx.x.x`                |

例如，MiniMax Music 3 API 的 `0.1.0` 版本使用：

```text
verdantflare-app:music-minimax-music3-api-v0.1.0
```

版本号遵循 `MAJOR.MINOR.PATCH` 格式。已发布的版本 tag 不得覆盖，生产部署必须引用明确版本，不使用 `latest` 等浮动 tag。

## 当前实现

`music-minimax-music3-api` v0.1.0 锁定 MiniMax Music 3 模型 Revision 和 SGLang-Omni 0.1.3，按两张可见 CUDA GPU 部署。服务采用 OpenAI 兼容的 `POST /v1/audio/speech` 和 `GET /health`；官方运行时的预期输出为 32 kHz、16-bit、双声道 WAV。

构建、运行、生成请求、资源要求和许可证约束见 [`services/music-minimax-music3-api/README.md`](services/music-minimax-music3-api/README.md)。该版本只完成模型推理边界；项目资产登记、候选任务编排和 MCP 工具不在此服务内实现。

当前已完成静态检查、模型 Revision 核对和冒烟脚本的模拟响应测试。必须在目标服务器完成镜像构建、双 RTX 4090 启动和真实 10 秒生成测试后，才能将该服务标记为已验证。

`music-rvc-api` 提供同步训练验收、模型发现和音色转换；模型 ID 不可覆盖，训练返回模型、索引和真实转换得到的 15 秒验证音频。它不包含 WebUI、UVR5 或任务数据库。详见 [`services/music-rvc-api/README.md`](services/music-rvc-api/README.md)。

`music-uvr5-api` 使用锁定的 `audio-separator`、MelBand RoFormer 和独立去混响模型，返回 24-bit/48 kHz 的 `instrumental.wav` 与 `vocal_dry_original.wav`。`music-audio-mixer-api` 使用 Pedalboard 和 FFmpeg 生成母带 WAV、320 kbps MP3，并验证、原样打包调用方提供的 LRC。

`music-mcp-server` 暴露五个 `v1-draft` 结构化工具，不传输媒体 Base64，不接收宿主机路径，也不复制 Station 的 Task/Attempt/Artifact/AssetVersion。端到端输入、执行命令和全部验收输出见 [`docs/acceptance.md`](docs/acceptance.md)。

当前静态、单元和契约测试不等于音频质量验收。只有在目标 GPU 跑完真实端到端脚本并完成人工审核后，才能确认模型效果、显存需求、处理时间和最终母带参数。

## Kubernetes 验证环境

真实 GPU、模型和音频端到端验证固定使用 Kubernetes context `chengdu.beagle` 和专用 namespace `verdantflare-music`。所有命令显式指定 context，不修改本机全局 current-context。该 namespace 使用 `hami.io/webhook=ignore` 绕过 HAMI 份额调度，由默认 NVIDIA device-plugin 分配完整物理 GPU。

声明式清单包括：

- `namespace.yaml`：项目隔离边界。
- `storage.yaml`：Music3、UVR5 和 RVC 独立 JuiceFS 模型卷，回收策略由集群 `juicefs` StorageClass 控制。
- `services.yaml`：五个只在集群内部暴露的 ClusterIP Service。
- `workloads.yaml`：五个版本化服务 Deployment。
- `gpu-probe.yaml`：在下载 Music3 模型前验证同一 Pod 可见两个不同 RTX 4090 UUID 的一次性 Job。

Music3 申请两张完整 RTX 4090，并使用 32 GiB 内存型 `/dev/shm` 和 host IPC；UVR5 与 RVC 各申请一张完整 RTX 4090。GPU workload 同时要求 `NVIDIA-GeForce-RTX-4090` 和 `nvidia.com/device-plugin.config=default` 节点标签，避开 HAMI 份额节点，但不会固化临时空闲节点地址。

### 部署门禁

部署前必须同时满足：

1. 五个版本镜像均由对应 `release` workflow 成功发布。当前 Music3 `v0.1.0` 的历史 run 为 `startup_failure`，重新构建成功前不得启动完整部署；其余四个服务的当前版本流水线已成功。
2. `verdantflare-music` namespace 内存在私有仓库拉取 Secret `beagle-registry`。Secret 由集群管理员提供，不进入本仓库。
3. `juicefs` StorageClass 可用，且为模型卷保留至少 170 GiB 容量。
4. 至少四张完整 RTX 4090 可用于同时运行 Music3、UVR5 和 RVC；只验证单个服务时可分别部署。
5. 已准备 `docs/acceptance.md` 要求的批准企划、歌词、音乐描述、真人录音和 LRC。

先创建隔离 namespace：

```bash
kubectl --context chengdu.beagle apply \
  -f deploy/chengdu.beagle/verdantflare-music/namespace.yaml
```

双 GPU 探测使用公开 CUDA 镜像，不依赖应用镜像仓库凭据：

```bash
kubectl --context chengdu.beagle apply \
  -f deploy/chengdu.beagle/verdantflare-music/gpu-probe.yaml
kubectl --context chengdu.beagle -n verdantflare-music \
  wait --for=condition=complete job/music3-dual-gpu-probe --timeout=10m
kubectl --context chengdu.beagle -n verdantflare-music \
  logs job/music3-dual-gpu-probe
```

日志必须恰好包含两个不同 GPU UUID。探测通过、全部镜像发布且管理员已将
`beagle-registry` Secret 放入该 namespace 后部署服务：

```bash
kubectl --context chengdu.beagle apply -k \
  deploy/chengdu.beagle/verdantflare-music
kubectl --context chengdu.beagle -n verdantflare-music \
  rollout status deployment --all --timeout=60m
```

验收脚本从本机通过端口转发访问四个执行 API：

```bash
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-minimax-music3-api 8001:8000
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-uvr5-api 8002:8000
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-rvc-api 8003:8000
kubectl --context chengdu.beagle -n verdantflare-music \
  port-forward service/music-audio-mixer-api 8004:8000
```

四条端口转发需要在独立终端持续运行。随后按 [`docs/acceptance.md`](docs/acceptance.md) 设置真实输入并执行 `scripts/acceptance-music-workflow.sh`。脚本格式验收通过后仍需完成五个人工审核点，才能推进 `main`。

## 运行数据

源码仓库只保存代码、契约、部署配置和小型非媒体测试数据，不得保存原始录音、生成音频、人声模型、模型权重、缓存或交付文件。

Station 在运行时提供项目范围存储和模型存储。部署可以挂载以下逻辑目录：

```text
/data/verdantflare/music/
├── projects/
├── voice-models/
├── model-cache/
└── temp/
```

服务通过 Station 边界接收项目和资产资源句柄，不得接受不受限制的宿主机文件系统路径。生成文件应登记为 Task Artifact 和 AssetVersion，而不是提交到 Git。

## 开发规则

- `dev` 用于日常开发与集成，`release` 用于发布，`main` 保存已验收的稳定代码。
- 只允许按 `dev -> release -> main` 顺序快进分支，不得创建合并提交或改写历史。
- `dev` 和 `main` 不运行 CI；推送 `release` 时按变更的服务执行检查、构建和镜像推送。
- 为全部五个服务构建并发布由 VerdantFlare 维护的镜像。
- 测试、镜像构建和 Kubernetes 发布统一通过 CI/CD 流水线执行。
- 锁定上游源码、Python 依赖、基础镜像和模型 Revision。
- 保留上游许可证和署名声明。
- 禁止将 Secret、凭据、媒体文件、模型权重和运行数据提交到 Git。
- 只有对应执行路径已经实现并通过测试后，才能添加启动命令。
- 先跑通一条最小端到端链路，再扩展完整制作流程。

## 发布流程

发布前必须确认工作区干净、`dev` 已提交并与远端同步，并且不存在未完成的 merge、rebase 或分支分叉。版本号必须先在 `dev` 中更新；已发布的镜像版本不可覆盖。

将 `dev` 快进到 `release`，触发对应服务的 CI：

```bash
git checkout release
git merge dev --ff-only
git push origin release
```

镜像构建和目标服务器验收全部通过后，将同一提交快进到 `main`，然后切回 `dev`：

```bash
git checkout main
git merge release --ff-only
git push origin main
git checkout dev
```

任一步骤无法快进时必须停止处理，不得强推或改写分支历史。

## 设计来源

- [AI 音乐制作人工作流](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music.md)
- [私有化部署与 MCP 技术方案](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music/AI%E9%9F%B3%E4%B9%90%E5%88%B6%E4%BD%9C%E4%BA%BA_0.%E6%8A%80%E6%9C%AF%E6%96%B9%E6%A1%88.md)

设计文档描述目标产品。模型可用性、本地推理支持、GPU 占用、处理延迟和输出质量等内容，必须先在本仓库中完成验证，才能作为实现事实。
