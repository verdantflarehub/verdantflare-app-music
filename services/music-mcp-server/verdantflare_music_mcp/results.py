from __future__ import annotations

import json

from mcp import types

from .artifacts import ArtifactRecord, ArtifactStore


def artifact_result(
    store: ArtifactStore,
    project_id: str,
    operation: str,
    records: list[ArtifactRecord],
) -> types.CallToolResult:
    artifacts: list[dict[str, object]] = []
    content: list[types.ContentBlock] = []
    for record in records:
        download_url = store.download_url(record.artifact_id)
        item: dict[str, object] = {
            "artifact_id": record.artifact_id,
            "filename": record.filename,
            "media_type": record.media_type,
            "size": record.size,
            "sha256": record.sha256,
            "download_path": store.download_path(record.artifact_id),
        }
        if download_url is not None:
            item["download_url"] = download_url
            content.append(
                types.ResourceLink(
                    name=record.filename,
                    uri=download_url,
                    mimeType=record.media_type,
                    size=record.size,
                )
            )
        artifacts.append(item)

    structured = {
        "status": "completed",
        "project_id": project_id,
        "operation": operation,
        "artifacts": artifacts,
    }
    content.insert(0, types.TextContent(type="text", text=json.dumps(structured, ensure_ascii=False)))
    return types.CallToolResult(content=content, structuredContent=structured)
