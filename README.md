# VerdantFlare App Music

VerdantFlare App Music 是部署在 VerdantFlare Station 上的 AI 音乐制作应用套件。它通过版本化的 MCP 工具和内部 HTTP API，提供音乐生成、音轨分离、人声模型训练、音色转换、混音与母带处理能力。

本仓库采用逐服务增量开发。`music-minimax-music3-api` 已完成首个运行时镜像实现，等待目标 GPU 服务器实机验收；其余服务和完整生产部署栈仍在建设中。

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

规划中的 MCP 能力与服务边界一一对应：

```text
music.generate
stems.separate
voice.train
voice.convert
mix.master
```

在 `contracts/mcp/` 中完成契约评审和版本化之前，以上工具名和 Schema 均为暂定内容。

## 仓库结构

规划中的源码目录按可独立部署的服务组织：

```text
.
├── .github/
│   └── workflows/
├── README.md
├── AGENTS.md
├── .env.example
├── services/
│   ├── music-mcp-server/
│   ├── music-minimax-music3-api/
│   ├── music-uvr5-api/
│   ├── music-rvc-api/
│   └── music-audio-mixer-api/
├── contracts/
│   ├── http/
│   └── mcp/
├── deploy/
│   └── kubernetes/
└── scripts/
```

自研 Python 服务使用相同的内部目录结构：

```text
services/<service>/
├── Dockerfile
├── pyproject.toml
├── src/<python_package>/
└── tests/
```

服务依赖、镜像构建文件和测试保留在各自服务目录内。直接采用成熟上游运行时的服务不重复包装 Python 应用，只保存锁定版本的 Dockerfile、启动入口、许可证和验收脚本。跨服务 HTTP 和 MCP Schema 统一放在 `contracts/`。只有在实际存在需要消除的跨服务重复代码时，才引入共享 Python 包。

`.github/workflows/` 负责测试、构建五个版本化服务镜像、推送镜像仓库，以及触发 Kubernetes 滚动发布。`deploy/kubernetes/` 保存流水线引用的 Kubernetes 部署资源。

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
