"""Project-scoped asynchronous DSP jobs with persistent idempotency receipts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import uuid
import zipfile

from .artifacts import require_project_id
from .results import structured_result
from .review_rules import MUSIC_REVIEW_RULES
from . import continuation


BACKEND_OPERATIONS = {
    "voice.tune": "vocal.tune",
    "voice.process": "vocal.process",
    "music.instrument_render": "instrument.render",
    "mix.render": "mix.render",
}


class ProductionJobs:
    def __init__(self, executor, emit):
        self.executor = executor
        self.store = executor.store
        self.emit = emit
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="production")
        self.lock = threading.RLock()
        self.epoch = uuid.uuid4().hex

    def _path(self, project_id, request_id):
        project = require_project_id(project_id)
        request = str(uuid.UUID(request_id))
        root = self.store.root / "production-jobs" / project
        root.mkdir(parents=True, exist_ok=True)
        return root / (request + ".json")

    def _save(self, path, data):
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def status(self, project_id, request_id, cancel=False):
        path = self._path(project_id, request_id)
        with self.lock:
            if not path.exists():
                raise ValueError("production request not found in project")
            job = json.loads(path.read_text())
            if job["epoch"] != self.epoch and job["state"] in {
                "queued",
                "running",
                "cancelling",
            }:
                job["state"] = "interrupted"
                job["error"] = (
                    "worker restarted; outputs were not published; inspect before a new request"
                )
                self._save(path, job)
            if cancel and job["state"] in {"queued", "running"}:
                job["state"] = "cancelled" if job["state"] == "queued" else "cancelling"
                self._save(path, job)
            for item in job.get("artifacts", []):
                item["download_path"] = self.store.download_path(item["artifact_id"])
            return {
                k: v
                for k, v in job.items()
                if k not in {"epoch", "fingerprint", "config"}
            }

    def submit(self, project_id, request_id, operation, config, asset_ids):
        path = self._path(project_id, request_id)
        if (
            not isinstance(config, dict)
            or len(json.dumps(config, allow_nan=False)) > 512000
            or len(asset_ids) > 16
        ):
            raise ValueError("invalid production configuration")
        if operation not in BACKEND_OPERATIONS and operation != "music.extend":
            raise ValueError("unsupported production operation")
        if operation == "music.extend":
            if len(asset_ids) != 1:
                raise ValueError("continuation requires exactly one source")
            continuation.validate(self.executor, project_id, asset_ids[0], config)
        # Resolve every input inside the project before accepting work.
        inputs = [self.store.get(a, project_id=project_id) for a in asset_ids]
        payload = {
            "operation": operation,
            "config": config,
            "asset_ids": asset_ids,
            "inputs_sha256": [r.sha256 for r in inputs],
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        with self.lock:
            if path.exists():
                old = json.loads(path.read_text())
                if old["fingerprint"] != fingerprint:
                    raise ValueError("request_id already used with different inputs")
                return self.status(project_id, request_id)
            active = sum(
                json.loads(p.read_text()).get("state")
                in {"queued", "running", "cancelling"}
                for p in (self.store.root / "production-jobs").glob("*/*.json")
                if json.loads(p.read_text()).get("epoch") == self.epoch
            )
            if active >= 8:
                raise ValueError("project production queue is full")
            job = {
                **payload,
                "project_id": project_id,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "epoch": self.epoch,
                "state": "queued",
                "artifacts": [],
            }
            self._save(path, job)
            self.pool.submit(self._run, path)
        return self.status(project_id, request_id)

    def resume_extension(self, project_id, request_id):
        path = self._path(project_id, request_id)
        with self.lock:
            status = self.status(project_id, request_id)
            job = json.loads(path.read_text())
            if job["operation"] != "music.extend":
                raise ValueError("only continuation tasks support backend-result recovery")
            if job["state"] in {"completed", "queued", "running", "cancelling", "cancelled"}:
                return status
            if not job.get("backend_task_id") or job.get("artifacts"):
                raise ValueError("no recoverable backend identity or partial publication requires inspection")
            job.update(state="queued", epoch=self.epoch)
            job.pop("error", None)
            self._save(path, job)
            self.pool.submit(self._run, path)
            return self.status(project_id, request_id)

    def _run(self, path):
        # Protect shared-volume deployments against concurrent process execution.
        with path.with_suffix(".lock").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            with self.lock:
                job = json.loads(path.read_text())
                if job["state"] != "queued":
                    return
                job["state"] = "running"
                self._save(path, job)
            try:
                if job["operation"] == "music.extend":
                    def submitted(task_id):
                        with self.lock:
                            current = json.loads(path.read_text())
                            current["backend_task_id"] = task_id
                            job["backend_task_id"] = task_id
                            self._save(path, current)
                    values = continuation.render(
                        self.executor, job["project_id"], job["asset_ids"][0],
                        job["config"], submitted, resume_task_id=job.get("backend_task_id"),
                    )
                else:
                    files = []
                    for asset_id in job["asset_ids"]:
                        rec, payload = self.store.read(asset_id, job["project_id"])
                        files.append(("audio", (rec.filename, payload, rec.media_type)))
                    raw = self.executor._post(
                        "Production",
                        self.executor.service_urls.mixer + "/v1/production/render",
                        data={
                            "operation": BACKEND_OPERATIONS[job["operation"]],
                            "config_json": json.dumps(job["config"], allow_nan=False),
                        },
                        files=files or [("placeholder", (None, ""))],
                    )
                    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                        names = archive.namelist()
                        allowed = {"render.wav", "dry.wav", "wet.wav", "report.json"}
                        if (
                            not {"render.wav", "report.json"} <= set(names)
                            or len(names) != len(set(names))
                            or not set(names) <= allowed
                        ):
                            raise ValueError("invalid production archive")
                        if sum(i.file_size for i in archive.infolist()) > 1024**3:
                            raise ValueError("production output exceeds limit")
                        values = {name: archive.read(name) for name in names}
                report = json.loads(values["report.json"])
                for name, payload in values.items():
                    if (
                        name != "report.json"
                        and report.get("outputs_sha256", {}).get(name)
                        != hashlib.sha256(payload).hexdigest()
                    ):
                        raise ValueError("production output checksum mismatch")
                with self.lock:
                    current = json.loads(path.read_text())
                    if current["state"] == "cancelling":
                        current["state"] = "cancelled"
                        self._save(path, current)
                        return
                    records = []
                    for name, payload in values.items():
                        records.append(
                            self.store.create(
                                project_id=job["project_id"],
                                operation=job["operation"],
                                filename=name,
                                media_type=(
                                    "application/json"
                                    if name.endswith(".json")
                                    else "audio/wav"
                                ),
                                payload=payload,
                            )
                        )
                        # Keep published identities recoverable even if the process stops mid-publication.
                        job["artifacts"] = [r.model_dump() for r in records]
                        self._save(path, job)
                    self.emit(
                        job["project_id"],
                        job["operation"],
                        records,
                        {"request_id": job["request_id"], "config": job["config"]},
                    )
                    job["state"] = "completed"
                    self._save(path, job)
            except Exception as error:
                with self.lock:
                    current = json.loads(path.read_text())
                    current["state"] = (
                        "cancelled" if current["state"] == "cancelling" else "failed"
                    )
                    current["error"] = (
                        type(error).__name__
                        + ": production failed; inspect configuration and backend health"
                    )
                    self._save(path, current)


def register(mcp, executor, emit):
    jobs = ProductionJobs(executor, emit)

    @mcp.tool(name="music.production_capabilities")
    def production_capabilities():
        """Read live JSON Schemas and limits for voice.tune/process, music.instrument_render and mix.render. Config must match the selected schema."""
        result = executor._get_json(
            "Production", executor.service_urls.mixer + "/v1/production/capabilities"
        )
        result["schemas"] = {
            public: result["schemas"][backend]
            for public, backend in BACKEND_OPERATIONS.items()
        }
        return structured_result(result)

    @mcp.tool(name="music.production_status")
    def production_status(project_id: str, request_id: str):
        """Recover a production request and its immutable artifacts. Poll the original request_id; never resubmit unknown work."""
        return structured_result(jobs.status(project_id, request_id))

    @mcp.tool(name="music.production_cancel")
    def production_cancel(project_id: str, request_id: str):
        """Cancel queued work or suppress output publication after an active bounded DSP call returns. Cancelling is not yet cancelled."""
        return structured_result(jobs.status(project_id, request_id, cancel=True))

    @mcp.tool(name="music.extend_resume")
    def music_extend_resume(project_id: str, request_id: str):
        """Recover a failed/interrupted continuation from its recorded backend task. Never submits new inference or changes inputs. Completed/cancelled/active requests are returned unchanged. Partial publication requires inspection."""
        return structured_result(jobs.resume_extension(project_id, request_id))

    @mcp.tool(name="music.extend", description=(
        "Queue source-conditioned end continuation with ACE-Step. Adds 3-60 seconds, "
        "total at most 300 seconds; preserves every source PCM frame. Source must be "
        "project-owned mono/stereo PCM16/24 WAV. Lyrics describe the new section. "
        "Use workflow.preflight(redraw) first and poll music.production_status using "
        "the same request_id. Interrupted jobs never auto-regenerate. The new section "
        "needs musical audition before dependent extension; prefix integrity does not "
        "guarantee melody, voice or arrangement continuity.\n\n" + MUSIC_REVIEW_RULES))
    def music_extend(
        project_id: str, request_id: str, audio_asset_id: str,
        addition_seconds: float, instructions: str, lyrics: str = "", seed: int = 7,
    ):
        return structured_result(jobs.submit(
            project_id, request_id, "music.extend",
            {"addition_seconds": addition_seconds, "instructions": instructions,
             "lyrics": lyrics, "seed": seed}, [audio_asset_id],
        ))

    @mcp.tool(name="voice.tune")
    def vocal_tune(project_id: str, request_id: str, audio_asset_id: str, config: dict):
        """Queue independent WORLD pitch correction. Read music.production_capabilities for config, including target notes, scale, retune speed and formants."""
        return structured_result(
            jobs.submit(project_id, request_id, "voice.tune", config, [audio_asset_id])
        )

    @mcp.tool(name="voice.process")
    def vocal_process(
        project_id: str,
        request_id: str,
        audio_asset_id: str,
        config: dict,
        carrier_asset_id: str | None = None,
    ):
        """Queue an ordered vocal effect chain. Vocoder requires a separate carrier artifact; no reference-singer cloning. Discover config via music.production_capabilities."""
        return structured_result(
            jobs.submit(
                project_id,
                request_id,
                "voice.process",
                config,
                [audio_asset_id] + ([carrier_asset_id] if carrier_asset_id else []),
            )
        )

    @mcp.tool(name="music.instrument_render", description='Render an original synth stem from timed notes. Read music.production_capabilities; derive timing and harmony from the actual source when adding to an existing performance.' + "\n\n" + MUSIC_REVIEW_RULES)
    def instrument_render(project_id: str, request_id: str, config: dict):
        """Queue a reproducible original synth stem from timed MIDI-number notes, envelope, waveform and filter controls. Read music.production_capabilities first."""
        return structured_result(
            jobs.submit(project_id, request_id, "music.instrument_render", config, [])
        )

    @mcp.tool(name="mix.render", description='Queue explicit multitrack premix without automatic mastering or vocal processing. Config tracks match artifact order; read music.production_capabilities.' + "\n\n" + MUSIC_REVIEW_RULES)
    def mix_render(
        project_id: str, request_id: str, audio_asset_ids: list[str], config: dict
    ):
        """Queue explicit multitrack premix without automatic mastering or vocal processing. Config tracks match artifact order; read music.production_capabilities."""
        return structured_result(
            jobs.submit(project_id, request_id, "mix.render", config, audio_asset_ids)
        )

    return jobs
