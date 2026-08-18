# VerdantFlare App Music

VerdantFlare App Music 是部署在 VerdantFlare Station 上的 AI 音乐制作应用套件。它通过版本化的 MCP 工具和内部 HTTP API，提供音乐生成、音轨分离、人声模型训练、音色转换、混音与母带处理能力。

本仓库目前处于初始设计和工程脚手架阶段，尚未提供可运行的生产环境服务栈。

## 项目职责

本仓库负责在 VerdantFlare Station 上运行的以下服务：

| 服务                  | 职责                                                                |
| --------------------- | ------------------------------------------------------------------- |
| `music-mcp-server`    | 在 Station MCP 边界暴露音乐制作工具，校验请求并向内部服务提交任务。 |
| `music-generator-api` | 提供稳定的内部候选歌曲生成 API，并隔离具体的模型运行时。            |
| `uvr5-separator-api`  | 提供音轨分离、去混响、模型加载和任务状态 API。                      |
| `rvc-engine-api`      | 提供人声模型训练、模型管理和音色转换 API。                          |
| `audio-mixer-api`     | 提供混音、母带处理、响度标准化和交付格式编码 API。                  |

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
├── README.md
├── AGENTS.md
├── Makefile
├── compose.yaml
├── .env.example
├── services/
│   ├── music-mcp-server/
│   ├── music-generator-api/
│   ├── uvr5-separator-api/
│   ├── rvc-engine-api/
│   └── audio-mixer-api/
├── contracts/
│   ├── http/
│   └── mcp/
├── deploy/
│   ├── compose/
│   └── station/
├── tests/
│   └── e2e/
└── scripts/
```

每个 Python 服务使用相同的内部目录结构：

```text
services/<service>/
├── Dockerfile
├── pyproject.toml
├── src/<python_package>/
└── tests/
```

服务依赖、容器构建和单元测试保留在各自服务目录内。跨服务 HTTP 和 MCP Schema 统一放在 `contracts/`。只有在实际存在需要消除的跨服务重复代码时，才引入共享 Python 包。

UVR5 和 RVC 是由本仓库构建和发布的一等服务。项目可以使用锁定版本的成熟上游实现作为算法依赖，但上游公开的 WebUI、CLI 或容器接口不是 VerdantFlare 的服务契约。

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

- 在 `dev` 分支开发，`main` 作为稳定分支。
- 为全部五个服务构建并发布由 VerdantFlare 维护的镜像。
- 锁定上游源码、Python 依赖、基础镜像和模型 Revision。
- 保留上游许可证和署名声明。
- 禁止将 Secret、凭据、媒体文件、模型权重和运行数据提交到 Git。
- 只有对应执行路径已经实现并通过测试后，才能添加启动命令。
- 先跑通一条最小端到端链路，再扩展完整制作流程。

## 设计来源

- [AI 音乐制作人工作流](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music.md)
- [私有化部署与 MCP 技术方案](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/design/workflow/verdantflare_music/AI%E9%9F%B3%E4%B9%90%E5%88%B6%E4%BD%9C%E4%BA%BA_0.%E6%8A%80%E6%9C%AF%E6%96%B9%E6%A1%88.md)

设计文档描述目标产品。模型可用性、本地推理支持、GPU 占用、处理延迟和输出质量等内容，必须先在本仓库中完成验证，才能作为实现事实。
