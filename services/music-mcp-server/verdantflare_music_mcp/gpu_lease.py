"""Shared GPU task lease and on-demand scale.

Approved 2026-10-19 for 5090 Music3, H3, and IndexTTS2. Write this role's
Lease first, then read peers. A live conflicting peer means release and retry.
H3 and IndexTTS2 do not exclude each other. Music3 and IndexTTS2 scale to 1
while held and back to 0 after the idle window. H3 stays resident.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

LEASES = {
    "music3": "gpu-task-music3",
    "h3": "gpu-task-h3",
    "indextts2": "gpu-task-indextts2",
}
CONFLICTS = {
    "music3": ("h3", "indextts2"),
    "h3": ("music3",),
    "indextts2": ("music3",),
}


class LeaseBusy(RuntimeError):
    def __init__(self, holder: str | None = None) -> None:
        self.holder = holder or ""
        super().__init__("waiting_gpu_lease")


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def replicas_from_scale(payload: dict) -> int:
    # Kubernetes omits spec.replicas when the desired count is zero.
    spec = payload.get("spec") or {}
    if "replicas" not in spec:
        return 0
    return int(spec["replicas"])


def held_by_other(lease: dict | None, holder: str, now: datetime) -> str | None:
    spec = (lease or {}).get("spec") or {}
    identity = spec.get("holderIdentity") or ""
    if not identity or identity == holder:
        return None
    renew = spec.get("renewTime")
    duration = int(spec.get("leaseDurationSeconds") or 60)
    if not renew:
        return identity
    if parse_time(renew) + timedelta(seconds=duration) > now:
        return identity
    return None


class MemoryApi:
    """In-memory Lease and scale API for tests."""

    def __init__(self) -> None:
        self.leases: dict[str, dict] = {}
        self.scales: dict[str, int] = {}

    def get_lease(self, name: str) -> dict | None:
        item = self.leases.get(name)
        return None if item is None else {
            "metadata": dict(item["metadata"]),
            "spec": dict(item["spec"]),
        }

    def replace_lease(self, name: str, spec: dict, resource_version: str | None) -> int:
        current = self.leases.get(name)
        current_version = None if current is None else current["metadata"].get("resourceVersion")
        if current is not None and current_version != resource_version:
            return 409
        version = str(int(current_version or "0") + 1)
        self.leases[name] = {"metadata": {"name": name, "resourceVersion": version}, "spec": dict(spec)}
        return 200

    def get_scale(self, name: str) -> int:
        return self.scales.get(name, 0)

    def patch_scale(self, name: str, replicas: int) -> None:
        self.scales[name] = replicas


class K8sApi:
    def __init__(
        self,
        namespace: str,
        host: str = "https://kubernetes.default.svc",
        token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
        ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
    ) -> None:
        self.namespace = namespace
        self.host = host.rstrip("/")
        self.token_path = token_path
        self.ca_path = ca_path

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        token = Path(self.token_path).read_text(encoding="utf-8").strip()
        headers = {"Authorization": f"Bearer {token}"}
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update(extra)
        with httpx.Client(verify=self.ca_path, timeout=10, trust_env=False) as client:
            return client.request(method, self.host + path, headers=headers, **kwargs)

    def get_lease(self, name: str) -> dict | None:
        path = f"/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases/{name}"
        response = self._request("GET", path)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def replace_lease(self, name: str, spec: dict, resource_version: str | None) -> int:
        path = f"/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases/{name}"
        body = {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": spec,
        }
        if resource_version:
            body["metadata"]["resourceVersion"] = resource_version
            response = self._request("PUT", path, json=body)
        else:
            response = self._request("POST", f"/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases", json=body)
            # A competing creator owns the new resource. Re-observe it in the
            # admission loop; never turn a failed create into an overwrite.
        if response.status_code in {200, 201}:
            return response.status_code
        return response.status_code

    def get_scale(self, name: str) -> int:
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}/scale"
        response = self._request("GET", path)
        response.raise_for_status()
        return replicas_from_scale(response.json())

    def patch_scale(self, name: str, replicas: int) -> None:
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}/scale"
        response = self._request(
            "PATCH",
            path,
            json={"spec": {"replicas": replicas}},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        response.raise_for_status()


class GpuCoordinator:
    def __init__(
        self,
        api: object | None,
        role: str,
        holder: str,
        deployment: str | None = None,
        enabled: bool = False,
        idle_seconds: int = 600,
        queue_seconds: int = 1200,
        ready_seconds: int = 900,
        lease_seconds: int = 90,
        now=None,
        sleep=None,
        monotonic=None,
    ) -> None:
        self.api = api
        self.role = role
        self.holder = holder
        self.deployment = deployment or None
        self.enabled = enabled
        self.idle_seconds = idle_seconds
        self.queue_seconds = queue_seconds
        self.ready_seconds = ready_seconds
        self.lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self.in_flight = 0
        self.idle_at: float | None = None
        self._lock = threading.RLock()

    @property
    def lease_name(self) -> str:
        return LEASES[self.role]

    @classmethod
    def from_environment(cls) -> "GpuCoordinator":
        role = os.environ.get("GPU_TASK_ROLE", "").strip()
        if role not in CONFLICTS:
            return cls(api=None, role="", holder="", enabled=False)
        namespace = os.environ.get("GPU_TASK_NAMESPACE", "").strip()
        namespace_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        if not namespace and namespace_file.is_file():
            namespace = namespace_file.read_text(encoding="utf-8").strip()
        namespace = namespace or "verdantflare-app"
        holder = os.environ.get("GPU_TASK_HOLDER", "").strip() or f"{role}/{os.environ.get('HOSTNAME', 'local')}"
        deployment = os.environ.get("GPU_TASK_DEPLOYMENT", "").strip() or None
        return cls(
            api=K8sApi(namespace),
            role=role,
            holder=holder,
            deployment=deployment,
            enabled=True,
            idle_seconds=int(os.environ.get("GPU_IDLE_SECONDS", "600")),
            queue_seconds=int(os.environ.get("GPU_QUEUE_SECONDS", "1200")),
            ready_seconds=int(os.environ.get("GPU_READY_SECONDS", "900")),
            lease_seconds=int(os.environ.get("GPU_LEASE_SECONDS", "90")),
        )

    def peer_holder(self) -> str | None:
        if not self.enabled:
            return None
        now = self._now()
        for peer in CONFLICTS[self.role]:
            holder = held_by_other(self.api.get_lease(LEASES[peer]), self.holder, now)
            if holder:
                return holder
        return None

    def api_ok(self) -> bool:
        if not self.enabled:
            return True
        try:
            self.api.get_lease(self.lease_name)
            return True
        except Exception:
            return False

    def replicas(self) -> int | None:
        if not self.enabled or not self.deployment:
            return None
        try:
            return int(self.api.get_scale(self.deployment))
        except Exception:
            return None

    def replicas_for(self, deployment: str | None) -> int | None:
        if self.api is None or not deployment:
            return None
        try:
            return int(self.api.get_scale(deployment))
        except Exception:
            return None

    def scale_to(self, replicas: int) -> None:
        if not self.enabled or not self.deployment:
            return
        if self.api.get_scale(self.deployment) != replicas:
            self.api.patch_scale(self.deployment, replicas)

    def scale_deployment_to(self, deployment: str | None, replicas: int) -> None:
        if self.api is None or not deployment:
            return
        if self.api.get_scale(deployment) != replicas:
            self.api.patch_scale(deployment, replicas)

    def _spec(self, holder: str, acquire_time: str | None = None) -> dict:
        now = format_time(self._now())
        return {
            "holderIdentity": holder,
            "leaseDurationSeconds": self.lease_seconds,
            "acquireTime": acquire_time or now,
            "renewTime": now,
        }

    def _write(self, holder: str) -> bool:
        current = self.api.get_lease(self.lease_name)
        identity = ((current or {}).get("spec") or {}).get("holderIdentity")
        if not holder and identity != self.holder:
            return False
        if holder and held_by_other(current, self.holder, self._now()):
            return False
        version = None if current is None else current.get("metadata", {}).get("resourceVersion")
        acquire_time = None
        if current and (current.get("spec") or {}).get("holderIdentity") == self.holder:
            acquire_time = (current.get("spec") or {}).get("acquireTime")
        return self.api.replace_lease(self.lease_name, self._spec(holder, acquire_time), version) in {200, 201}

    def try_acquire(self) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            now = self._now()
            current = self.api.get_lease(self.lease_name)
            if held_by_other(current, self.holder, now):
                return False
            if self.in_flight:
                # An overlapping call joins the existing holder. A peer's
                # transient admission claim must not revoke running work.
                if ((current or {}).get("spec") or {}).get("holderIdentity") != self.holder:
                    return False
                return self._write(self.holder)
            if not self._write(self.holder):
                return False
            if self.peer_holder():
                self._write("")
                return False
            return True

    def try_acquire_hold(self) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            if self.try_acquire():
                self.in_flight += 1
                self.idle_at = None
                return True
            return False

    def acquire_for_queue(self) -> None:
        if not self.enabled:
            return
        deadline = self._monotonic() + self.queue_seconds
        delay = 0.2
        while True:
            with self._lock:
                if self.try_acquire():
                    self.in_flight += 1
                    self.idle_at = None
                    return
            if self._monotonic() >= deadline:
                raise LeaseBusy(self.peer_holder())
            delay = min(5.0, delay * 2)
            self._sleep(delay * (0.5 + random.random()))

    def renew(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if not self.in_flight:
                return
            current = self.api.get_lease(self.lease_name)
            identity = ((current or {}).get("spec") or {}).get("holderIdentity")
            if identity != self.holder:
                return
            self._write(self.holder)

    def release(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self.in_flight:
                self.in_flight -= 1
            if self.in_flight:
                return
            self._write("")
            if self.idle_at is None:
                self.idle_at = self._monotonic()

    def maybe_scale_down(self) -> None:
        if not self.enabled or not self.deployment:
            return
        with self._lock:
            if self.in_flight or self.idle_at is None:
                return
            if self._monotonic() - self.idle_at < self.idle_seconds:
                return
            current = self.api.get_lease(self.lease_name)
            if ((current or {}).get("spec") or {}).get("holderIdentity") == self.holder:
                return
            # The same CAS lease fences other replicas while the scale write
            # runs; the local lock also fences a new call in this process.
            if not self._write(self.holder):
                return
            try:
                if self.replicas() not in {None, 0}:
                    self.scale_to(0)
            finally:
                self._write("")

    def hold(self) -> "_Hold":
        return _Hold(self)


class _Hold:
    def __init__(self, coord: GpuCoordinator) -> None:
        self.coord = coord
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Hold":
        self.coord.acquire_for_queue()
        if self.coord.enabled:
            self._thread = threading.Thread(target=self._renew, daemon=True)
            self._thread.start()
        return self

    def _renew(self) -> None:
        interval = max(5, self.coord.lease_seconds // 3)
        while not self._stop.wait(interval):
            try:
                self.coord.renew()
            except Exception:
                pass

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        self.coord.release()
        return False


SCHEDULER_GRANTED_STATES = frozenset({"leased", "running", "granted"})

SCHEDULER_CONSUMER_SPECS: dict[str, dict[str, object]] = {
    "music3": {
        "consumer_kind": "music3",
        "stage_id": "music3_generate",
        "card_count": 2,
        "peak_budget_mib": 16384,
        "priority_class": "exclusive_batch",
        "deployment": "music-minimax-music3",
    },
    "uvr5": {
        "consumer_kind": "uvr5",
        "stage_id": "uvr5_separate",
        "card_count": 1,
        "peak_budget_mib": 6144,
        "priority_class": "shared_slice",
        "deployment": "music-uvr5",
    },
    "rvc": {
        "consumer_kind": "rvc",
        "stage_id": "rvc_voice",
        "card_count": 1,
        "peak_budget_mib": 8192,
        "priority_class": "shared_slice",
        "deployment": "music-rvc",
    },
    "ace-step": {
        "consumer_kind": "ace-step",
        "stage_id": "ace_step_editor",
        "card_count": 1,
        "peak_budget_mib": 12288,
        "priority_class": "shared_slice",
        "deployment": "music-ace-step",
    },
}


def is_lease_granted(lease: dict | None) -> bool:
    return isinstance(lease, dict) and str(lease.get("state") or "") in SCHEDULER_GRANTED_STATES


class SchedulerError(RuntimeError):
    def __init__(self, code: str, message: str = "", status_code: int = 500) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message or code)


def _fsync_directory(dir_path: Path) -> None:
    if os.name == "nt":
        return
    dfd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class SchedulerLeaseClient:
    """Station Runtime :5056 GPU scheduler client for music-mcp (Music3 2-card + UVR5/RVC/ACE-Step slices)."""

    def __init__(
        self,
        *,
        base_url: str = "",
        token: str = "",
        holder_app_id: str = "music-mcp",
        enabled: bool | None = None,
        ttl_seconds: int = 90,
        queue_seconds: int = 1200,
        pending_releases_path: str | Path | None = None,
        client: httpx.Client | None = None,
        sleep=None,
        monotonic=None,
    ) -> None:
        self.base_url = base_url.strip().rstrip("/")
        self.token = token.strip()
        self.holder_app_id = holder_app_id.strip() or "music-mcp"
        self.enabled = bool(self.base_url and self.token) if enabled is None else bool(enabled and self.base_url)
        self.ttl_seconds = int(ttl_seconds)
        self.queue_seconds = int(queue_seconds)
        raw_journal = str(pending_releases_path).strip() if pending_releases_path is not None else ""
        self._pending_releases_path = Path(raw_journal) if raw_journal else None
        self._client = client
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._pending_lock = threading.Lock()
        self._journal_corrupted = False
        self._journal_error = ""
        self._pending_releases: dict[str, dict] = {}
        self._load_pending_releases_locked()

    @classmethod
    def from_environment(cls) -> "SchedulerLeaseClient":
        url = os.environ.get("STATION_GPU_SCHEDULER_URL", "").strip()
        tok = (
            os.environ.get("STATION_GPU_SCHEDULER_TOKEN", "").strip()
            or os.environ.get("STATION_RUNTIME_GPU_TOKEN", "").strip()
        )
        journal_path = os.environ.get("STATION_GPU_PENDING_RELEASES_PATH", "").strip()
        if not journal_path:
            default_dir = Path("/data/music-mcp") if Path("/data").is_dir() else Path(tempfile.gettempdir())
            journal_path = str(default_dir / "gpu_pending_releases.json")
        return cls(
            base_url=url,
            token=tok,
            holder_app_id=os.environ.get("STATION_GPU_HOLDER_APP_ID", "music-mcp").strip() or "music-mcp",
            ttl_seconds=int(os.environ.get("GPU_LEASE_SECONDS", "90")),
            queue_seconds=int(os.environ.get("GPU_QUEUE_SECONDS", "1200")),
            pending_releases_path=journal_path,
        )

    def _ensure_journal_healthy_locked(self) -> None:
        if self._journal_corrupted:
            raise SchedulerError("JOURNAL_CORRUPTED", self._journal_error or "pending_releases_journal_corrupted", 503)

    def _load_pending_releases_locked(self) -> None:
        if self._pending_releases_path is None or not os.path.lexists(self._pending_releases_path):
            return
        try:
            if os.path.islink(self._pending_releases_path) or not self._pending_releases_path.is_file():
                raise ValueError("journal_not_regular_file")
            payload = json.loads(self._pending_releases_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("journal_payload_not_dict")
            entries = payload.get("pending_releases", payload)
            if not isinstance(entries, dict):
                raise ValueError("journal_entries_not_dict")
            loaded: dict[str, dict] = {}
            for lid, body in entries.items():
                key = str(lid).strip()
                if not key or not isinstance(body, dict) or int(body.get("fencing_token") or 0) <= 0:
                    raise ValueError(f"invalid_pending_release_entry:{lid!r}")
                clean_body = dict(body)
                clean_body.pop("expected_version", None)
                loaded[key] = clean_body
            self._pending_releases = loaded
        except Exception as exc:
            self._journal_corrupted = True
            self._journal_error = f"pending_releases_journal_corrupted:{type(exc).__name__}:{exc}"

    def _persist_pending_releases_locked(self, next_entries: dict[str, dict]) -> None:
        self._ensure_journal_healthy_locked()
        if self._pending_releases_path is not None:
            tmp_path = self._pending_releases_path.with_name(
                f"{self._pending_releases_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self._pending_releases_path.parent.mkdir(parents=True, exist_ok=True)
                data = json.dumps({"pending_releases": next_entries}, separators=(",", ":")).encode("utf-8")
                fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short_write_zero_bytes")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(str(tmp_path), str(self._pending_releases_path))
                _fsync_directory(self._pending_releases_path.parent)
            except Exception as exc:
                try:
                    if os.path.lexists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass
                self._journal_corrupted = True
                self._journal_error = f"pending_releases_persist_failed:{type(exc).__name__}:{exc}"
                raise SchedulerError("JOURNAL_PERSIST_FAILED", self._journal_error, 503) from exc
        self._pending_releases = next_entries

    @staticmethod
    def _is_confirmed_release_receipt(lease_id: str, data: object) -> bool:
        if not isinstance(data, dict):
            return False
        resp_lid = str(data.get("lease_id") or "").strip()
        if resp_lid != lease_id:
            return False
        return str(data.get("state") or "") in ("released", "expired")

    def _request_raw(self, method: str, path: str, json_body: dict | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        if self._client is not None:
            return self._client.request(method, self.base_url + path, headers=headers, json=json_body)
        with httpx.Client(timeout=15.0, trust_env=False) as http:
            return http.request(method, self.base_url + path, headers=headers, json=json_body)

    @staticmethod
    def _parse_error(resp: httpx.Response) -> SchedulerError:
        code = f"HTTP_{resp.status_code}"
        msg = resp.text
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if isinstance(data, dict):
            top_code = data.get("error_code")
            top_msg = data.get("message")
            err = data.get("error")
            if isinstance(top_code, str) and top_code:
                code = top_code
                if isinstance(top_msg, str) and top_msg:
                    msg = top_msg
            elif isinstance(err, dict):
                if isinstance(err.get("code"), str) and err["code"]:
                    code = err["code"]
                if isinstance(err.get("message"), str) and err["message"]:
                    msg = err["message"]
        return SchedulerError(code, msg, status_code=resp.status_code)

    def _request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        try:
            resp = self._request_raw(method, path, json_body)
        except Exception as exc:
            raise SchedulerError("SCHEDULER_TRANSPORT_FAILED", str(exc), 503) from exc
        if resp.status_code >= 400:
            raise self._parse_error(resp)
        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def get_pending_release(self, lease_id: str) -> dict | None:
        with self._pending_lock:
            self._ensure_journal_healthy_locked()
            entry = self._pending_releases.get(lease_id)
            return dict(entry) if isinstance(entry, dict) else None

    def flush_pending_releases(self, *, include_quarantined: bool = False) -> int:
        if not self.enabled:
            return 0
        with self._pending_lock:
            self._ensure_journal_healthy_locked()
            items = [
                (lid, dict(body))
                for lid, body in self._pending_releases.items()
                if include_quarantined or not bool(body.get("_quarantined_400"))
            ]
        if not items:
            return 0
        confirmed_ids: list[str] = []
        quarantined_updates: dict[str, str] = {}
        flushed = 0
        for lease_id, body in items:
            send_body = {
                k: v
                for k, v in body.items()
                if k != "expected_version" and not str(k).startswith("_")
            }
            try:
                resp = self._request_raw("POST", f"/internal/v1/gpu-scheduler/leases/{lease_id}/release", send_body)
                if resp.status_code == 200:
                    data = resp.json() if resp.content else {}
                    if self._is_confirmed_release_receipt(lease_id, data):
                        confirmed_ids.append(lease_id)
                        flushed += 1
                        continue
                elif resp.status_code == 400:
                    err = self._parse_error(resp)
                    logging.error(
                        "music_gpu_release_quarantined_400 lease_id=%s code=%s message=%s",
                        lease_id,
                        err.code,
                        err.message,
                    )
                    quarantined_updates[lease_id] = f"{err.code}:{err.message}"
                    continue
            except Exception:
                pass
        if confirmed_ids or quarantined_updates:
            with self._pending_lock:
                next_entries = dict(self._pending_releases)
                for lid in confirmed_ids:
                    next_entries.pop(lid, None)
                for lid, qerr in quarantined_updates.items():
                    if lid in next_entries and isinstance(next_entries[lid], dict):
                        upd = dict(next_entries[lid])
                        upd["_quarantined_400"] = True
                        upd["_last_error"] = qerr
                        next_entries[lid] = upd
                self._persist_pending_releases_locked(next_entries)
        return flushed

    def acquire(
        self,
        *,
        consumer_key: str,
        task_id: str,
        attempt_id: str,
        stage_id: str | None = None,
        runtime_identity: dict | None = None,
        k8s_slots_required: int = 0,
        idempotency_key: str | None = None,
    ) -> dict:
        self.flush_pending_releases()
        spec = SCHEDULER_CONSUMER_SPECS.get(consumer_key, SCHEDULER_CONSUMER_SPECS["music3"])
        resolved_stage = stage_id or str(spec["stage_id"])
        ident = runtime_identity or {}
        body: dict[str, object] = {
            "idempotency_key": idempotency_key or f"acq:{task_id}:{attempt_id}:{resolved_stage}",
            "holder_app_id": self.holder_app_id,
            "consumer_kind": str(spec["consumer_kind"]),
            "task_id": task_id,
            "attempt_id": attempt_id,
            "stage_id": resolved_stage,
            "card_count": int(spec["card_count"]),
            "peak_budget_mib": int(spec["peak_budget_mib"]),
            "priority_class": str(spec["priority_class"]),
            "ttl_seconds": self.ttl_seconds,
        }
        if k8s_slots_required > 0:
            body["k8s_slots_required"] = int(k8s_slots_required)
        for key in ("pod_uid", "container_id", "boot_id"):
            val = ident.get(key)
            if isinstance(val, str) and val and not val.startswith("unknown-"):
                body[key] = val
        visible = ident.get("visible_gpu_uuids")
        if isinstance(visible, list) and visible:
            body["candidate_gpu_uuids"] = [str(u) for u in visible if str(u)]
        return self._request("POST", "/internal/v1/gpu-scheduler/leases/acquire", body)

    def acquire_or_wait(
        self,
        *,
        consumer_key: str,
        task_id: str,
        attempt_id: str,
        stage_id: str | None = None,
        runtime_identity: dict | None = None,
        k8s_slots_required: int = 0,
    ) -> dict:
        deadline = self._monotonic() + self.queue_seconds
        delay = 0.5
        lease_doc = self.acquire(
            consumer_key=consumer_key,
            task_id=task_id,
            attempt_id=attempt_id,
            stage_id=stage_id,
            runtime_identity=runtime_identity,
            k8s_slots_required=k8s_slots_required,
        )
        while not is_lease_granted(lease_doc):
            state = str(lease_doc.get("state") or "")
            lease_id = str(lease_doc.get("lease_id") or "")
            if state not in {"queued", "draining"}:
                raise LeaseBusy(str(lease_doc.get("blocking_reason") or state))
            if self._monotonic() >= deadline:
                if lease_id:
                    try:
                        self.release(
                            lease_id,
                            int(lease_doc.get("fencing_token") or 0),
                            worker_settled=False,
                            reclaim_mode="abort_unstarted",
                        )
                    except Exception:
                        pass
                raise LeaseBusy(str(lease_doc.get("blocking_holder_id") or lease_doc.get("blocking_reason") or "scheduler_queue"))
            self._sleep(min(5.0, delay))
            delay = min(5.0, delay * 1.5)
            if lease_id:
                lease_doc = self.get_lease(lease_id)
            else:
                lease_doc = self.acquire(
                    consumer_key=consumer_key,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    stage_id=stage_id,
                    runtime_identity=runtime_identity,
                    k8s_slots_required=k8s_slots_required,
                )
        return lease_doc

    def get_lease(self, lease_id: str) -> dict:
        return self._request("GET", f"/internal/v1/gpu-scheduler/leases/{lease_id}")

    def bind_target(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        runtime_identity: dict,
        expected_version: int = 0,
        idempotency_key: str | None = None,
    ) -> dict:
        pod_uid = str(runtime_identity.get("pod_uid") or "").strip()
        container_id = str(runtime_identity.get("container_id") or "").strip()
        boot_id = str(runtime_identity.get("boot_id") or "").strip()
        if not pod_uid or not container_id or not boot_id or pod_uid.startswith("unknown-"):
            raise SchedulerError("INVALID_TARGET_IDENTITY", "bind_target requires concrete pod_uid, container_id, boot_id", 400)
        visible = runtime_identity.get("visible_gpu_uuids") or []
        actual_gpus = [str(u) for u in visible if str(u)] if isinstance(visible, (list, tuple)) else []
        body: dict[str, object] = {
            "idempotency_key": idempotency_key or f"bind:{lease_id}:{fencing_token}:{pod_uid}",
            "fencing_token": int(fencing_token),
            "ttl_seconds": self.ttl_seconds,
            "pod_uid": pod_uid,
            "container_id": container_id,
            "boot_id": boot_id,
        }
        if expected_version > 0:
            body["expected_version"] = int(expected_version)
        if actual_gpus:
            body["actual_gpu_uuids"] = actual_gpus
        return self._request("POST", f"/internal/v1/gpu-scheduler/leases/{lease_id}/bind-target", body)

    def heartbeat(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        expected_version: int = 0,
        idempotency_key: str | None = None,
    ) -> dict:
        body: dict[str, object] = {
            "idempotency_key": idempotency_key or f"hb:{lease_id}:{fencing_token}:{int(time.time() // 15)}",
            "fencing_token": int(fencing_token),
            "ttl_seconds": self.ttl_seconds,
        }
        if expected_version > 0:
            body["expected_version"] = int(expected_version)
        return self._request("POST", f"/internal/v1/gpu-scheduler/leases/{lease_id}/heartbeat", body)

    def release(
        self,
        lease_id: str,
        fencing_token: int,
        *,
        worker_settled: bool = True,
        reclaim_mode: str = "warm_idle_settled",
        reclaim_proof: dict | None = None,
        target_identity: dict | None = None,
        expected_version: int = 0,
        idempotency_key: str | None = None,
    ) -> dict:
        ident = target_identity or {}
        body: dict[str, object] = {
            "idempotency_key": idempotency_key or f"rel:{lease_id}:{fencing_token}:{reclaim_mode or 'plain'}",
            "fencing_token": int(fencing_token),
            "worker_settled": bool(worker_settled),
        }
        if expected_version > 0:
            body["expected_version"] = int(expected_version)
        if reclaim_mode:
            body["reclaim_mode"] = reclaim_mode
        if reclaim_proof is not None:
            body["reclaim_proof"] = reclaim_proof
        if isinstance(ident.get("supervisor_pid"), int) and ident["supervisor_pid"] > 0:
            body["supervisor_pid"] = int(ident["supervisor_pid"])
        if isinstance(ident.get("supervisor_starttime_ticks"), int) and ident["supervisor_starttime_ticks"] > 0:
            body["supervisor_starttime_ticks"] = int(ident["supervisor_starttime_ticks"])
        for key in ("pod_uid", "container_id", "boot_id"):
            val = ident.get(key)
            if isinstance(val, str) and val and not val.startswith("unknown-"):
                body[key] = val
        retry_body = dict(body)
        retry_body.pop("expected_version", None)
        with self._pending_lock:
            self._ensure_journal_healthy_locked()
            next_entries = dict(self._pending_releases)
            next_entries[lease_id] = retry_body
            self._persist_pending_releases_locked(next_entries)

        try:
            resp = self._request_raw("POST", f"/internal/v1/gpu-scheduler/leases/{lease_id}/release", body)
        except Exception as exc:
            raise SchedulerError("RELEASE_TRANSPORT_FAILED", str(exc), 503) from exc
        if resp.status_code != 200:
            err = self._parse_error(resp)
            if resp.status_code == 400:
                logging.error(
                    "music_gpu_release_quarantined_400 lease_id=%s code=%s message=%s",
                    lease_id,
                    err.code,
                    err.message,
                )
                with self._pending_lock:
                    next_entries = dict(self._pending_releases)
                    if lease_id in next_entries and isinstance(next_entries[lease_id], dict):
                        upd = dict(next_entries[lease_id])
                        upd["_quarantined_400"] = True
                        upd["_last_error"] = f"{err.code}:{err.message}"
                        next_entries[lease_id] = upd
                        self._persist_pending_releases_locked(next_entries)
            raise err
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            raise SchedulerError("RELEASE_INVALID_JSON", str(exc), 502) from exc
        if not self._is_confirmed_release_receipt(lease_id, data):
            raise SchedulerError(
                "RELEASE_NOT_TERMINAL",
                f"Unconfirmed release receipt for {lease_id!r}: {data!r}",
                502,
            )
        with self._pending_lock:
            next_entries = dict(self._pending_releases)
            if lease_id in next_entries:
                next_entries.pop(lease_id, None)
                self._persist_pending_releases_locked(next_entries)
        return data

    def build_grant_headers(
        self,
        lease: dict | None,
        *,
        parent_task_id: str,
        consumer_kind: str = "",
        stage_id: str = "",
        target_identity: dict | None = None,
    ) -> dict[str, str]:
        if not self.enabled or not is_lease_granted(lease):
            return {}
        ident = target_identity or {}
        assigned = lease.get("gpu_uuids") or lease.get("assigned_gpu_uuids") or []
        gpu_uuids = [str(u) for u in assigned if str(u)] if isinstance(assigned, (list, tuple)) else []
        if not gpu_uuids and lease.get("gpu_uuid"):
            gpu_uuids = [str(lease["gpu_uuid"])]
        visible = ident.get("visible_gpu_uuids") or []
        if not gpu_uuids and isinstance(visible, list) and visible:
            gpu_uuids = [str(u) for u in visible if str(u)]
        headers = {
            "X-VF-GPU-Lease-ID": str(lease["lease_id"]),
            "X-VF-GPU-Fencing-Token": str(lease["fencing_token"]),
            "X-VF-GPU-Attempt-ID": str(lease.get("attempt_id") or "att-1"),
            "X-VF-GPU-Stage-ID": str(stage_id or lease.get("stage_id") or "music"),
            "X-VF-GPU-Parent-Task-ID": str(parent_task_id),
            "X-VF-GPU-Consumer-Kind": str(consumer_kind or lease.get("consumer_kind") or "music"),
            "X-VF-GPU-Holder-App-ID": self.holder_app_id,
        }
        pod_uid = str(lease.get("pod_uid") or ident.get("pod_uid") or "")
        container_id = str(lease.get("container_id") or ident.get("container_id") or "")
        boot_id = str(lease.get("boot_id") or ident.get("boot_id") or "")
        if pod_uid and not pod_uid.startswith("unknown-"):
            headers["X-VF-GPU-Pod-UID"] = pod_uid
        if container_id and not container_id.startswith("unknown-"):
            headers["X-VF-GPU-Container-ID"] = container_id
        if boot_id and not boot_id.startswith("unknown-"):
            headers["X-VF-GPU-Boot-ID"] = boot_id
        if gpu_uuids:
            headers["X-VF-GPU-UUID"] = ",".join(gpu_uuids)
        expires_at = str(lease.get("expires_at") or "")
        if not expires_at:
            expires_at = format_time(datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds))
        headers["X-VF-GPU-Expires-At"] = expires_at
        return headers
