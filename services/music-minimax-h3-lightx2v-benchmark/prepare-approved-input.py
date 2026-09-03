#!/usr/bin/env python3
"""Build a LightX2V jobs file from one approved Video MCP task."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


TASK_ID = os.environ["H3_SOURCE_VIDEO_TASK_ID"]
PROJECT_ID = os.environ["H3_SOURCE_PROJECT_ID"]
IDEMPOTENCY_KEY = os.environ["H3_SOURCE_IDEMPOTENCY_KEY"]
ROOT = Path(os.environ.get("VIDEO_MCP_ROOT", "/data/projects/video-mcp"))
OUTPUT = Path(os.environ.get("H3_JOBS_JSON", "/inputs/ref2va.json"))
KINDS = (
    ("images", "image", "Picture"),
    ("videos", "video", "Video"),
    ("audios", "audio", "Audio"),
)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as source:
        return json.load(source)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    task = load_json(ROOT / "tasks" / f"{TASK_ID}.json")
    if task.get("status") != "succeeded":
        raise ValueError("source Video MCP task is not succeeded")
    if task.get("project_id") != PROJECT_ID or task.get("idempotency_key") != IDEMPOTENCY_KEY:
        raise ValueError("source Video MCP task identity does not match the approved input")

    request = task["request"]
    if request.get("model") != "minimax-h3-ref2va" or request.get("project_id") != PROJECT_ID:
        raise ValueError("source task is not the approved MiniMax H3 Ref2VA request")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lightx2v-input-", dir=OUTPUT.parent) as temporary:
        temporary_path = Path(temporary)
        references_path = temporary_path / "references"
        references_path.mkdir()
        references = []
        prompt_tags = []
        source_artifacts = []

        for plural, singular, tag in KINDS:
            for index, item in enumerate(request["references"].get(plural, []), start=1):
                artifact_id = item["artifact_id"]
                artifact_path = ROOT / "artifacts" / artifact_id
                metadata = load_json(artifact_path / "metadata.json")
                if metadata["project_id"] != PROJECT_ID or not metadata["media_type"].startswith(f"{singular}/"):
                    raise ValueError(f"invalid approved artifact: {artifact_id}")
                source = artifact_path / metadata["filename"]
                actual_sha256 = sha256(source)
                if actual_sha256 != metadata["sha256"]:
                    raise ValueError(f"artifact checksum mismatch: {artifact_id}")
                filename = f"{singular}-{index}{source.suffix.lower()}"
                shutil.copyfile(source, references_path / filename)
                references.append({"type": singular, "path": f"references/{filename}"})
                prompt_tags.append(f"<{tag} {index}> is the approved {item['purpose']} reference")
                source_artifacts.append({"artifact_id": artifact_id, "sha256": actual_sha256})

        if not references:
            raise ValueError("approved task has no references")
        jobs = {
            "examples": [{
                "task": "ref2va",
                "prompt": "; ".join(prompt_tags) + ". " + request["prompt"],
                "duration": request["duration_seconds"],
                "megapixels": 0.98,
                "aspect_ratio": request["aspect_ratio"],
                "references": references,
            }]
        }
        jobs_path = temporary_path / "ref2va.json"
        jobs_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit = {
            "source_video_task_id": TASK_ID,
            "source_input_digest": task["input_digest"],
            "source_artifacts": source_artifacts,
            "jobs_sha256": sha256(jobs_path),
        }
        (temporary_path / "source.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        for name in ("references", "ref2va.json", "source.json"):
            destination = OUTPUT.parent / name
            source = temporary_path / name
            if destination.is_dir():
                shutil.rmtree(destination)
            elif destination.exists():
                destination.unlink()
            os.replace(source, destination)

    print(f"Prepared approved LightX2V input: {OUTPUT}")


if __name__ == "__main__":
    main()
