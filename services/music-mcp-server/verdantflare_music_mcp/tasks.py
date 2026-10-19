from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any
import wave

from .artifacts import ArtifactRecord, ArtifactStore

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

OPERATION_WORKFLOW_MAP: dict[str, tuple[str, str, str, str, str, str]] = {
    "vocal.tune": ("full_song", "独立修音", "WORLD", "TUNE", "人声修音", "48kHz"),
    "vocal.process": ("full_song", "人声效果", "Production DSP", "FX", "电子人声", "48kHz"),
    "instrument.render": ("full_song", "合成器分轨", "VF Synth", "SYNTH", "乐器渲染", "48kHz"),
    "mix.render": ("full_song", "多轨预混", "Production DSP", "MIX", "预混", "48kHz"),
    "music.generate": ("generate", "单曲生成", "MiniMax Music 3", "T2M", "文生音乐", "32kHz"),
    "music.redraw": ("redraw", "局部重绘", "ACE-Step 1.5", "R2M", "局部重绘", "48kHz"),
    "duet.plan": ("duet_generate", "合唱企划", "ACE-Step 1.5", "DUET", "双人对唱", "JSON"),
    "duet.instrumental": ("duet_generate", "合唱伴奏", "ACE-Step 1.5", "DUET", "双人对唱", "48kHz"),
    "duet.vocal": ("duet_generate", "合唱人声", "ACE-Step 1.5", "DUET", "双人对唱", "48kHz"),
    "duet.preview": ("duet_generate", "合唱试听", "ACE-Step 1.5", "DUET", "双人对唱", "48kHz"),
    "stems.separate": ("stems_separate", "分轨分离", "UVR5 RoFormer", "STEM", "分轨分离", "44.1kHz"),
    "voice.prepare": ("voice_training", "声线预处理", "RVC v2", "RVC", "声线转换", "40kHz"),
    "voice.train": ("voice_training", "声线训练", "RVC v2", "RVC", "声线转换", "40kHz"),
    "voice.convert": ("cover", "声线转换", "RVC v2", "RVC", "声线转换", "40kHz"),
    "lyrics.align": ("full_song", "歌词对齐", "Whisper Small", "LRC", "歌词对齐", "LRC+JSON"),
    "mix.master": ("full_song", "母带混音", "Pedalboard Mixer", "MIX", "母带混音", "48kHz"),
    "asset.import": ("generate", "素材导入", "Artifact Store", "T2M", "素材导入", "WAV"),
}

PROJECT_DISPLAY_NAMES: dict[str, str] = {
    "mengsk-comfort": "孟斯克《算了吧》舒适声线版",
    "mengsk-error": "孟斯克《算了吧》演示工程",
    "mengsk-demo": "孟斯克声线样本工程",
}


class TaskNotFound(KeyError):
    pass


class InvalidTaskTitle(ValueError):
    pass


def _parse_iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _probe_wav_info(path: Path) -> tuple[float | None, str | None]:
    if not path.is_file() or path.suffix.lower() != ".wav":
        return None, None
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            frames = w.getnframes()
            dur = round(frames / rate, 2) if (rate > 0 and frames > 0) else None
            if rate > 0:
                khz = rate / 1000.0
                rate_str = f"{int(khz)}kHz" if khz.is_integer() else f"{khz:.1f}kHz"
            else:
                rate_str = None
            return dur, rate_str
    except Exception:
        return None, None


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "—"
    total = int(round(seconds))
    mins, secs = divmod(total, 60)
    return f"{mins:02d}:{secs:02d}"


def _default_title(project_id: str, operation: str, filenames: list[str], voice_model_id: str | None) -> str:
    proj_label = PROJECT_DISPLAY_NAMES.get(project_id, project_id)
    _, wf_label, _, _, _, _ = OPERATION_WORKFLOW_MAP.get(
        operation, ("generate", operation, "Music MCP", "T2M", operation, "WAV")
    )
    primary = filenames[0] if filenames else operation
    stem = Path(primary).stem.replace("_", " ")
    if voice_model_id:
        return f"{proj_label} · {wf_label} ({voice_model_id})"
    return f"{proj_label} · {wf_label} · {stem}"


class TaskStore:
    """Persistent task projection store backed by tasks.json and ArtifactStore discovery."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.tasks_file = self.store.root / "tasks.json"
        self._lock = threading.RLock()

    def _load_raw(self) -> dict[str, dict[str, Any]]:
        if not self.tasks_file.is_file():
            return {}
        try:
            payload = json.loads(self.tasks_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("tasks"), dict):
                return payload["tasks"]
        except Exception:
            pass
        return {}

    def _save_raw(self, tasks: dict[str, dict[str, Any]]) -> None:
        self.store.ensure_ready()
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "tasks": tasks,
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=".tasks-", dir=self.store.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, self.tasks_file)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _scan_all_artifacts(self) -> list[ArtifactRecord]:
        if not self.store.artifacts_root.is_dir():
            return []
        records: list[ArtifactRecord] = []
        for entry in sorted(self.store.artifacts_root.iterdir()):
            if not entry.is_dir() or not entry.name.startswith("art_"):
                continue
            meta = entry / "metadata.json"
            if not meta.is_file() or meta.is_symlink():
                continue
            try:
                rec = ArtifactRecord.model_validate_json(meta.read_text(encoding="utf-8"))
                records.append(rec)
            except Exception:
                continue
        records.sort(key=lambda r: (r.created_at, r.artifact_id))
        return records

    def sync_from_artifacts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            tasks = self._load_raw()
            indexed_artifacts: set[str] = set()
            for task in tasks.values():
                for art in task.get("artifacts", []):
                    if isinstance(art, dict) and isinstance(art.get("artifact_id"), str):
                        indexed_artifacts.add(art["artifact_id"])

            all_records = self._scan_all_artifacts()
            unindexed = [r for r in all_records if r.artifact_id not in indexed_artifacts]
            if not unindexed:
                return tasks

            # Group unindexed artifacts by (project_id, operation) within 90s windows
            groups: list[list[ArtifactRecord]] = []
            for rec in unindexed:
                if rec.operation == "duet.task":
                    continue
                if groups:
                    prev_group = groups[-1]
                    last = prev_group[-1]
                    if (
                        last.project_id == rec.project_id
                        and last.operation == rec.operation
                        and abs(_parse_iso(rec.created_at) - _parse_iso(last.created_at)) <= 90.0
                    ):
                        prev_group.append(rec)
                        continue
                groups.append([rec])

            changed = False
            for group in groups:
                first = group[0]
                digest_seed = "|".join(r.artifact_id for r in group)
                short_hash = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:12]
                task_id = f"task_{short_hash}"
                if task_id in tasks:
                    continue
                tasks[task_id] = self._build_task_dict(
                    task_id=task_id,
                    project_id=first.project_id,
                    operation=first.operation,
                    records=group,
                    params={},
                )
                changed = True

            if changed:
                self._save_raw(tasks)
            return tasks

    def _build_task_dict(
        self,
        *,
        task_id: str,
        project_id: str,
        operation: str,
        records: list[ArtifactRecord],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        workflow_key, workflow_label, default_model, type_code, type_label, default_spec = OPERATION_WORKFLOW_MAP.get(
            operation, ("generate", operation, "Music MCP", "T2M", operation, "WAV")
        )
        voice_model_id = params.get("model_id") or params.get("voice_model_id")
        if not voice_model_id and "mengsk" in project_id.lower() and operation in {"voice.convert", "mix.master"}:
            voice_model_id = "mengsk-comfort-v2-20260901"

        duration_seconds: float | None = None
        probed_spec: str | None = None
        lrc_text: str | None = params.get("lyrics_lrc") or None
        artifacts_list: list[dict[str, Any]] = []

        for r in records:
            cpath = self.store.artifacts_root / r.artifact_id / r.filename
            if duration_seconds is None and r.media_type.startswith("audio/"):
                duration_seconds, probed_spec = _probe_wav_info(cpath)
            if lrc_text is None and (r.filename.lower().endswith(".lrc") or r.operation == "lyrics.align"):
                if cpath.is_file():
                    try:
                        lrc_text = cpath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
            artifacts_list.append(
                {
                    "artifact_id": r.artifact_id,
                    "filename": r.filename,
                    "media_type": r.media_type,
                    "size": r.size,
                    "sha256": r.sha256,
                    "operation": r.operation,
                    "created_at": r.created_at,
                    "download_path": self.store.download_path(r.artifact_id),
                }
            )

        filenames = [r.filename for r in records]
        title = _default_title(project_id, operation, filenames, voice_model_id)
        created_at = records[0].created_at if records else datetime.now(UTC).isoformat()
        updated_at = records[-1].created_at if records else created_at
        spec_label = probed_spec or default_spec
        audio_length = f"{int(round(duration_seconds))}s" if duration_seconds and duration_seconds > 0 else "—"

        raw_prompt = (
            params.get("instructions")
            or params.get("style")
            or params.get("lyrics")
            or ""
        )
        if not raw_prompt:
            if voice_model_id:
                raw_prompt = f"[{operation}] Project {project_id} · voice_model_id={voice_model_id} · outputs: {', '.join(filenames)}"
            else:
                raw_prompt = f"[{operation}] Project {project_id} · outputs: {', '.join(filenames)}"

        return {
            "task_id": task_id,
            "title": title,
            "title_revision": 1,
            "project_id": project_id,
            "operation": operation,
            "workflow": workflow_key,
            "workflow_label": workflow_label,
            "type_code": type_code,
            "type_label": type_label,
            "spec_label": spec_label,
            "audio_length": audio_length,
            "channel_label": "本地",
            "status": "completed",
            "status_label": "已完成",
            "model": default_model,
            "voice_model_id": voice_model_id,
            "bpm": params.get("bpm"),
            "prompt": str(raw_prompt)[:280],
            "duration_seconds": duration_seconds,
            "duration_formatted": _format_duration(duration_seconds),
            "duration_Formatted": _format_duration(duration_seconds),
            "lyrics_lrc": lrc_text,
            "created_at": created_at,
            "updated_at": updated_at,
            "artifacts": artifacts_list,
        }

    def record_operation(
        self,
        *,
        project_id: str,
        operation: str,
        records: list[ArtifactRecord],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not records:
            return {}
        with self._lock:
            tasks = self.sync_from_artifacts()
            digest_seed = "|".join(r.artifact_id for r in records)
            short_hash = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()[:12]
            task_id = f"task_{short_hash}"
            existing = tasks.get(task_id)
            built = self._build_task_dict(
                task_id=task_id,
                project_id=project_id,
                operation=operation,
                records=records,
                params=params or {},
            )
            if existing:
                built["title"] = existing.get("title", built["title"])
                built["title_revision"] = existing.get("title_revision", 1)
            tasks[task_id] = built
            self._save_raw(tasks)
            return built

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks = self.sync_from_artifacts()
            items = list(tasks.values())
            items.sort(key=lambda t: (t.get("created_at", ""), t.get("task_id", "")), reverse=True)
            return items

    def get_task(self, task_id: str) -> dict[str, Any]:
        value = task_id.strip()
        if not TASK_ID_PATTERN.fullmatch(value):
            raise TaskNotFound(value)
        with self._lock:
            tasks = self.sync_from_artifacts()
            task = tasks.get(value)
            if task is None:
                raise TaskNotFound(value)
            # If task has no lyrics_lrc yet, check if project has any aligned LRC artifact
            if not task.get("lyrics_lrc"):
                for other in tasks.values():
                    if other.get("project_id") == task.get("project_id") and other.get("lyrics_lrc"):
                        task = {**task, "lyrics_lrc": other["lyrics_lrc"]}
                        break
            return task

    def rename_task(self, task_id: str, title: str) -> dict[str, Any]:
        value = task_id.strip()
        if not TASK_ID_PATTERN.fullmatch(value):
            raise TaskNotFound(value)
        cleaned = title.strip()
        if not 1 <= len(cleaned) <= 80 or any(ord(ch) < 32 for ch in cleaned):
            raise InvalidTaskTitle("title must be 1-80 printable characters")
        with self._lock:
            tasks = self.sync_from_artifacts()
            task = tasks.get(value)
            if task is None:
                raise TaskNotFound(value)
            task["title"] = cleaned
            task["title_revision"] = int(task.get("title_revision", 1)) + 1
            task["updated_at"] = datetime.now(UTC).isoformat()
            tasks[value] = task
            self._save_raw(tasks)
            return task
