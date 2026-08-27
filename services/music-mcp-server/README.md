# Music MCP Server（草案）

Streamable HTTP MCP 入口位于 `/mcp`，暴露 `music.generate`、`stems.separate`、`voice.train`、`voice.convert`、`mix.master` 五个结构化工具。

工具输出是内部 HTTP 调用描述和预期 Artifact，不携带媒体 Base64 或宿主机路径。Station Bridge 必须把 `asset_id` 解析为受控 multipart 文件流，并负责 Task、Attempt、Artifact 和 AssetVersion；本服务不建立重复的任务或资产数据库。

该合约标记为 `v1-draft`。Station 资源句柄解析接口接通并完成集成验收后才能定稿。
