from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel


PROJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ARTIFACT_ID_PATTERN = re.compile(r"art_[0-9a-f]{32}")
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024


class ArtifactError(RuntimeError):
    pass


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactRecord(BaseModel):
    schema_version: int = 1
    artifact_id: str
    project_id: str
    operation: str
    filename: str
    media_type: str
    size: int
    sha256: str
    created_at: str


def require_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not PROJECT_ID_PATTERN.fullmatch(value):
        raise ValueError("project_id must contain 1-64 ASCII letters, digits, dots, underscores, or hyphens")
    return value


def require_artifact_id(artifact_id: str) -> str:
    value = artifact_id.strip()
    if not ARTIFACT_ID_PATTERN.fullmatch(value):
        raise ValueError("artifact_id is invalid")
    return value


def require_filename(filename: str) -> str:
    value = filename.strip()
    if not value or len(value) > 128 or Path(value).name != value or value in {".", ".."}:
        raise ValueError("filename must be a safe basename of at most 128 characters")
    return value


class ArtifactStore:
    def __init__(self, root: Path, public_base_url: str | None = None) -> None:
        self.root = root.resolve()
        self.artifacts_root = self.root / "artifacts"
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None

    @classmethod
    def from_environment(cls) -> "ArtifactStore":
        base_url = os.environ.get("MUSIC_MCP_PUBLIC_BASE_URL", "").strip() or None
        return cls(Path(os.environ.get("MUSIC_ARTIFACT_ROOT", "/data/projects")), base_url)

    def ensure_ready(self) -> None:
        self.artifacts_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        if not self.artifacts_root.is_dir():
            raise ArtifactError("artifact root is unavailable")

    def create(
        self,
        *,
        project_id: str,
        operation: str,
        filename: str,
        media_type: str,
        payload: bytes,
    ) -> ArtifactRecord:
        if not payload:
            raise ArtifactError("artifact payload is empty")
        return self.create_from_chunks(
            project_id=project_id,
            operation=operation,
            filename=filename,
            media_type=media_type,
            chunks=(payload,),
        )

    def create_from_chunks(
        self,
        *,
        project_id: str,
        operation: str,
        filename: str,
        media_type: str,
        chunks: Iterable[bytes],
        expected_sha256: str | None = None,
    ) -> ArtifactRecord:
        project = require_project_id(project_id)
        safe_filename = require_filename(filename)

        self.ensure_ready()
        artifact_id = f"art_{uuid.uuid4().hex}"
        final_directory = self.artifacts_root / artifact_id
        temporary_directory = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.artifacts_root))
        try:
            content_path = temporary_directory / safe_filename
            size = 0
            digest = hashlib.sha256()
            with content_path.open("xb") as destination:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise ArtifactError("artifact exceeds 1 GiB")
                    destination.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise ArtifactError("artifact payload is empty")
            sha256 = digest.hexdigest()
            if expected_sha256 is not None and sha256 != expected_sha256:
                raise ArtifactError("artifact SHA-256 does not match expected_sha256")
            record = ArtifactRecord(
                artifact_id=artifact_id,
                project_id=project,
                operation=operation,
                filename=safe_filename,
                media_type=media_type,
                size=size,
                sha256=sha256,
                created_at=datetime.now(UTC).isoformat(),
            )
            (temporary_directory / "metadata.json").write_text(
                record.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_directory, final_directory)
            return record
        except Exception:
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise

    def get(self, artifact_id: str, project_id: str | None = None) -> ArtifactRecord:
        value = require_artifact_id(artifact_id)
        metadata_path = self.artifacts_root / value / "metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ArtifactNotFound("artifact does not exist")
        try:
            record = ArtifactRecord.model_validate_json(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ArtifactError("artifact metadata is invalid") from error
        if record.artifact_id != value:
            raise ArtifactError("artifact metadata does not match its directory")
        if project_id is not None and record.project_id != require_project_id(project_id):
            raise ArtifactNotFound("artifact does not exist in this project")
        return record

    def content_path(self, record: ArtifactRecord) -> Path:
        directory = (self.artifacts_root / require_artifact_id(record.artifact_id)).resolve()
        path = directory / require_filename(record.filename)
        if path.is_symlink() or not path.is_file() or directory.parent != self.artifacts_root:
            raise ArtifactNotFound("artifact content does not exist")
        return path

    def read(self, artifact_id: str, project_id: str) -> tuple[ArtifactRecord, bytes]:
        record = self.get(artifact_id, project_id)
        payload = self.content_path(record).read_bytes()
        if len(payload) != record.size or hashlib.sha256(payload).hexdigest() != record.sha256:
            raise ArtifactError("artifact content failed integrity verification")
        return record, payload

    def download_path(self, artifact_id: str) -> str:
        return f"/artifacts/{require_artifact_id(artifact_id)}/content"

    def download_url(self, artifact_id: str) -> str | None:
        if self.public_base_url is None:
            return None
        return f"{self.public_base_url}{self.download_path(artifact_id)}"
