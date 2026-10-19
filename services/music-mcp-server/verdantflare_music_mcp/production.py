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
                files = []
                for asset_id in job["asset_ids"]:
                    rec, payload = self.store.read(asset_id, job["project_id"])
                    files.append(("audio", (rec.filename, payload, rec.media_type)))
                raw = self.executor._post(
                    "Production",
                    self.executor.service_urls.mixer + "/v1/production/render",
                    data={
                        "operation": job["operation"],
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

    @mcp.tool(name="production.capabilities")
    def production_capabilities():
        """Read live JSON Schemas and limits for vocal.tune/process, instrument.render and mix.render. Config must match the selected schema."""
        return structured_result(
            executor._get_json(
                "Production",
                executor.service_urls.mixer + "/v1/production/capabilities",
            )
        )

    @mcp.tool(name="production.status")
    def production_status(project_id: str, request_id: str):
        """Recover a production request and its immutable artifacts. Poll the original request_id; never resubmit unknown work."""
        return structured_result(jobs.status(project_id, request_id))

    @mcp.tool(name="production.cancel")
    def production_cancel(project_id: str, request_id: str):
        """Cancel queued work or suppress output publication after an active bounded DSP call returns. Cancelling is not yet cancelled."""
        return structured_result(jobs.status(project_id, request_id, cancel=True))

    @mcp.tool(name="vocal.tune")
    def vocal_tune(project_id: str, request_id: str, audio_asset_id: str, config: dict):
        """Queue independent WORLD pitch correction. Read production.capabilities for config, including target notes, scale, retune speed and formants."""
        return structured_result(
            jobs.submit(project_id, request_id, "vocal.tune", config, [audio_asset_id])
        )

    @mcp.tool(name="vocal.process")
    def vocal_process(
        project_id: str,
        request_id: str,
        audio_asset_id: str,
        config: dict,
        carrier_asset_id: str | None = None,
    ):
        """Queue an ordered vocal effect chain. Vocoder requires a separate carrier artifact; no reference-singer cloning. Discover config via production.capabilities."""
        return structured_result(
            jobs.submit(
                project_id,
                request_id,
                "vocal.process",
                config,
                [audio_asset_id] + ([carrier_asset_id] if carrier_asset_id else []),
            )
        )

    @mcp.tool(name="instrument.render")
    def instrument_render(project_id: str, request_id: str, config: dict):
        """Queue a reproducible original synth stem from timed MIDI-number notes, envelope, waveform and filter controls. Read production.capabilities first."""
        return structured_result(
            jobs.submit(project_id, request_id, "instrument.render", config, [])
        )

    @mcp.tool(name="mix.render")
    def mix_render(
        project_id: str, request_id: str, audio_asset_ids: list[str], config: dict
    ):
        """Queue explicit multitrack premix without automatic mastering or vocal processing. Config tracks match artifact order; read production.capabilities."""
        return structured_result(
            jobs.submit(project_id, request_id, "mix.render", config, audio_asset_ids)
        )

    return jobs
