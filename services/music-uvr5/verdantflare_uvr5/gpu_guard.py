"""Receiver-side 6-tuple GPU grant validation, fail-closed durable fencing, and atomic warm reclaim proof for UVR5."""
from __future__ import annotations

from datetime import datetime
import hmac
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time


class StaleFenceError(RuntimeError):
    pass


class DuplicateInProgressError(RuntimeError):
    def __init__(self, request_id: str) -> None:
        super().__init__("submission_in_progress")
        self.request_id = request_id


class FencePersistenceError(RuntimeError):
    pass


def normalize_container_id(raw: str) -> str:
    val = (raw or "").strip()
    for prefix in ("containerd://", "docker://", "cri-o://"):
        if val.startswith(prefix):
            val = val[len(prefix) :].strip()
    return val


def is_unknown_identity_value(raw: str) -> bool:
    val = (raw or "").strip().lower()
    return (not val) or val.startswith("unknown") or val.startswith("local-")


def _fsync_directory(dir_path: Path) -> None:
    if os.name == "nt":
        return
    dfd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _read_proc_starttime_ticks() -> int:
    override = os.environ.get("VF_SUPERVISOR_STARTTIME_TICKS", "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    try:
        stat_raw = Path("/proc/self/stat").read_text(encoding="utf-8")
        rparen = stat_raw.rfind(")")
        if rparen > 0:
            fields = stat_raw[rparen + 2 :].split()
            if len(fields) > 19 and fields[19].isdigit():
                return int(fields[19])
    except OSError:
        pass
    return 1


def _detect_pod_and_container() -> tuple[str, str]:
    pod_uid = os.environ.get("POD_UID", "").strip()
    container_id = os.environ.get("CONTAINER_ID", "").strip()
    if pod_uid and container_id:
        return pod_uid, container_id
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        if not pod_uid:
            m_pod = re.search(r"/pods/([0-9a-fA-F_-]{8,64})/", mountinfo)
            if m_pod:
                pod_uid = m_pod.group(1).replace("_", "-")
        if not container_id:
            m_cid = re.search(r"/containers/([0-9a-f]{64})/", mountinfo)
            if m_cid:
                container_id = m_cid.group(1)
    except OSError:
        pass
    if not container_id:
        try:
            cgroup_text = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
            m_cg = re.search(r"([0-9a-f]{64})", cgroup_text)
            if m_cg:
                container_id = m_cg.group(1)
        except OSError:
            pass
    return pod_uid or "unknown-pod-uid", container_id or "unknown-container-id"


def _detect_boot_id() -> str:
    env_boot = os.environ.get("NODE_BOOT_ID", "").strip()
    if env_boot:
        return env_boot
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown-boot-id"


def _detect_visible_gpu_uuids() -> list[str]:
    override = os.environ.get("VF_VISIBLE_GPU_UUIDS", "").strip()
    if override:
        return [item.strip() for item in override.split(",") if item.strip()]
    env_vis = os.environ.get("NVIDIA_VISIBLE_DEVICES", "").strip()
    if env_vis and env_vis not in ("all", "void", "none"):
        concrete = [v.strip() for v in env_vis.split(",") if re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", v.strip())]
        if concrete:
            return concrete
    try:
        values = [
            entry.split(b"=", 1)[1].decode("ascii")
            for entry in Path("/proc/self/environ").read_bytes().split(b"\0")
            if entry.startswith(b"NVIDIA_VISIBLE_DEVICES=")
        ]
        concrete = [v for v in values if re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", v)]
        if concrete:
            return concrete
    except (OSError, UnicodeError):
        pass
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        return [line.strip() for line in out if re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", line.strip())]
    except (OSError, subprocess.SubprocessError):
        return []


def read_runtime_identity() -> dict[str, object]:
    pod_uid, container_id = _detect_pod_and_container()
    return {
        "pod_uid": pod_uid,
        "container_id": container_id,
        "boot_id": _detect_boot_id(),
        "supervisor_pid": os.getpid(),
        "supervisor_starttime_ticks": _read_proc_starttime_ticks(),
        "visible_gpu_uuids": _detect_visible_gpu_uuids(),
    }


def verify_internal_bearer(auth_header: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    candidate = auth_header.removeprefix("Bearer ").strip()
    if not candidate:
        return False
    for key in ("MUSIC_RUNTIME_TOKEN", "STATION_GPU_SCHEDULER_TOKEN"):
        expected = os.environ.get(key, "").strip()
        if expected and hmac.compare_digest(candidate, expected):
            return True
    return False


def _parse_expires_epoch(raw: str) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class LeaseFenceGuard:
    def __init__(
        self,
        *,
        policy_env: str = "MUSIC_GPU_GRANT_POLICY",
        state_path: str | Path | None = None,
        write_fn=None,
        fsync_fn=None,
        replace_fn=None,
        fsync_dir_fn=None,
    ) -> None:
        self._policy_env = policy_env
        raw_state = str(state_path).strip() if state_path is not None else os.environ.get("VF_GPU_GUARD_STATE_PATH", "").strip()
        self._state_path = Path(raw_state) if raw_state else None
        self._write_fn = write_fn or os.write
        self._fsync_fn = fsync_fn or os.fsync
        self._replace_fn = replace_fn or os.replace
        self._fsync_dir_fn = fsync_dir_fn or _fsync_directory
        self._lock = threading.RLock()
        self._corrupted = False
        self._corrupt_reason = ""
        self._min_fencing_token = 0
        self._consumed_tokens: set[int] = set()
        self._revoked_leases: set[str] = set()
        self._active_leases: dict[tuple[str, int], str] = {}
        self._attempt_digests: dict[tuple[str, str, str], str] = {}
        self._inflight_requests: dict[str, dict[str, object]] = {}
        self._unheadered_requests: set[str] = set()
        self._load_state_locked()

    @property
    def active_inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight_requests) + len(self._unheadered_requests)

    def _ensure_healthy_locked(self) -> None:
        if self._corrupted:
            raise FencePersistenceError(self._corrupt_reason or "fence_state_corrupted")

    def _load_state_locked(self) -> None:
        if self._state_path is None or not os.path.lexists(self._state_path):
            return
        try:
            if os.path.islink(self._state_path) or not self._state_path.is_file():
                raise ValueError("state_path_not_regular_file")
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("state_payload_not_dict")
            raw_min = payload.get("min_fencing_token", 0)
            if isinstance(raw_min, bool) or not isinstance(raw_min, int) or raw_min < 0:
                raise ValueError("invalid_min_fencing_token")
            raw_consumed = payload.get("consumed_tokens", [])
            if not isinstance(raw_consumed, list):
                raise ValueError("invalid_consumed_tokens")
            consumed: set[int] = set()
            for x in raw_consumed:
                if isinstance(x, bool) or not isinstance(x, int) or x <= 0:
                    raise ValueError("invalid_consumed_token_entry")
                consumed.add(x)
            raw_revoked = payload.get("revoked_leases", [])
            if not isinstance(raw_revoked, list):
                raise ValueError("invalid_revoked_leases")
            revoked: set[str] = set()
            for x in raw_revoked:
                if not isinstance(x, str) or not x.strip():
                    raise ValueError("invalid_revoked_lease_entry")
                revoked.add(x.strip())
            raw_digests = payload.get("attempt_digests", {})
            if not isinstance(raw_digests, dict):
                raise ValueError("invalid_attempt_digests")
            digests: dict[tuple[str, str, str], str] = {}
            for k, v in raw_digests.items():
                parts = str(k).split("\x1f")
                if len(parts) != 3 or not isinstance(v, str) or not v:
                    raise ValueError("invalid_attempt_digest_entry")
                digests[(parts[0], parts[1], parts[2])] = v
            raw_inflight = payload.get("inflight_requests", {})
            if not isinstance(raw_inflight, dict):
                raise ValueError("invalid_inflight_requests")
            cur_pid = os.getpid()
            cur_ticks = _read_proc_starttime_ticks()
            inflight: dict[str, dict[str, object]] = {}
            needs_repersist = False
            for rid, meta in raw_inflight.items():
                if not isinstance(rid, str) or not isinstance(meta, dict):
                    raise ValueError("invalid_inflight_request_entry")
                owner_pid = int(meta.get("owner_pid") or 0)
                owner_ticks = int(meta.get("owner_ticks") or 0)
                if owner_pid == cur_pid and owner_ticks == cur_ticks:
                    inflight[rid] = dict(meta)
                else:
                    lid = str(meta.get("lease_id") or "").strip()
                    tok = int(meta.get("fencing_token") or 0)
                    if lid:
                        revoked.add(lid)
                    if tok > 0:
                        consumed.add(tok)
                        if tok > raw_min:
                            raw_min = tok
                    needs_repersist = True
            self._min_fencing_token = raw_min
            self._consumed_tokens = consumed
            self._revoked_leases = revoked
            self._attempt_digests = digests
            self._inflight_requests = inflight
            if needs_repersist:
                self._commit_state_locked(
                    min_fencing_token=raw_min,
                    consumed_tokens=consumed,
                    revoked_leases=revoked,
                    attempt_digests=digests,
                    inflight_requests=inflight,
                )
        except Exception as exc:
            self._corrupted = True
            self._corrupt_reason = f"fence_state_corrupted:{type(exc).__name__}:{exc}"

    def _commit_state_locked(
        self,
        *,
        min_fencing_token: int,
        consumed_tokens: set[int],
        revoked_leases: set[str],
        attempt_digests: dict[tuple[str, str, str], str],
        inflight_requests: dict[str, dict[str, object]],
    ) -> None:
        self._ensure_healthy_locked()
        if self._state_path is not None:
            tmp_path = self._state_path.with_name(
                f"{self._state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                serialized_digests = {
                    f"{p}\x1f{a}\x1f{s}": d
                    for (p, a, s), d in attempt_digests.items()
                }
                data = json.dumps(
                    {
                        "min_fencing_token": min_fencing_token,
                        "consumed_tokens": sorted(consumed_tokens),
                        "revoked_leases": sorted(revoked_leases),
                        "attempt_digests": serialized_digests,
                        "inflight_requests": inflight_requests,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    view = memoryview(data)
                    while view:
                        written = self._write_fn(fd, view)
                        if written <= 0:
                            raise OSError("short_write_zero_bytes")
                        view = view[written:]
                    self._fsync_fn(fd)
                finally:
                    os.close(fd)
                self._replace_fn(str(tmp_path), str(self._state_path))
                self._fsync_dir_fn(self._state_path.parent)
            except Exception as exc:
                try:
                    if os.path.lexists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass
                self._corrupted = True
                self._corrupt_reason = f"fence_state_persist_failed:{type(exc).__name__}:{exc}"
                raise FencePersistenceError(self._corrupt_reason) from exc

        self._min_fencing_token = min_fencing_token
        self._consumed_tokens = consumed_tokens
        self._revoked_leases = revoked_leases
        self._attempt_digests = attempt_digests
        self._inflight_requests = inflight_requests

    @property
    def policy(self) -> str:
        raw = (
            os.environ.get(self._policy_env)
            or os.environ.get("VIDEO_GPU_GRANT_POLICY")
            or "disabled"
        ).strip().lower()
        if raw in ("enforce", "require", "enforcing"):
            return "enforce"
        if raw in ("warn", "accept"):
            return "warn"
        return "disabled"

    def extract_and_validate(
        self,
        headers,
        *,
        request_id: str,
        request_digest: str,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            self._ensure_healthy_locked()
        policy = self.policy
        if policy == "enforce" and self._state_path is None:
            raise FencePersistenceError("durable_state_path_required")
        norm = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        ident = identity or read_runtime_identity()
        lease_id = norm.get("x-vf-gpu-lease-id", "")
        token_raw = norm.get("x-vf-gpu-fencing-token", "")
        attempt_id = norm.get("x-vf-gpu-attempt-id", "")
        stage_id = norm.get("x-vf-gpu-stage-id", "")
        parent_task_id = norm.get("x-vf-gpu-parent-task-id", "") or request_id
        pod_uid = norm.get("x-vf-gpu-pod-uid", "")
        container_id = norm.get("x-vf-gpu-container-id", "")
        boot_id = norm.get("x-vf-gpu-boot-id", "")
        gpu_uuid = norm.get("x-vf-gpu-uuid", "")
        expires_raw = norm.get("x-vf-gpu-expires-at", "")

        has_any = any((lease_id, token_raw, attempt_id, stage_id, pod_uid, container_id, boot_id, gpu_uuid, expires_raw))
        if not has_any:
            if policy == "enforce":
                raise PermissionError("missing_or_invalid_gpu_grant")
            if policy == "warn":
                logging.warning("music_gpu_grant_missing_warn request_id=%s", request_id)
            with self._lock:
                self._ensure_healthy_locked()
                self._unheadered_requests.add(request_id)
            return None
        if not lease_id or not token_raw or not attempt_id or not stage_id:
            if policy == "enforce":
                raise PermissionError("missing_or_invalid_gpu_grant")
            with self._lock:
                self._ensure_healthy_locked()
                self._unheadered_requests.add(request_id)
            return None

        try:
            fencing_token = int(token_raw)
        except ValueError as exc:
            raise PermissionError("missing_or_invalid_gpu_grant") from exc
        if fencing_token <= 0:
            raise PermissionError("missing_or_invalid_gpu_grant")

        visible = list(ident.get("visible_gpu_uuids") or [])
        local_pod = str(ident.get("pod_uid") or "")
        local_cid = str(ident.get("container_id") or "")
        local_boot = str(ident.get("boot_id") or "")
        if policy == "enforce":
            if (
                is_unknown_identity_value(local_pod)
                or is_unknown_identity_value(local_cid)
                or is_unknown_identity_value(local_boot)
                or not visible
            ):
                raise PermissionError("gpu_identity_unavailable")
            if not all((pod_uid, container_id, boot_id, gpu_uuid, expires_raw)):
                raise PermissionError("missing_or_invalid_gpu_grant")

        if pod_uid and not is_unknown_identity_value(local_pod) and pod_uid != local_pod:
            raise PermissionError("pod_uid_mismatch")
        if container_id and not is_unknown_identity_value(local_cid):
            if normalize_container_id(container_id) != normalize_container_id(local_cid):
                raise PermissionError("container_id_mismatch")
        if boot_id and not is_unknown_identity_value(local_boot) and boot_id != local_boot:
            raise PermissionError("boot_id_mismatch")
        if gpu_uuid and visible:
            for g in (item.strip() for item in gpu_uuid.split(",") if item.strip()):
                if g not in visible:
                    raise PermissionError("gpu_uuid_mismatch")

        expires_epoch = _parse_expires_epoch(expires_raw)
        if expires_raw and expires_epoch is None:
            raise PermissionError("missing_or_invalid_gpu_grant")
        if expires_epoch is not None and expires_epoch <= time.time() and policy == "enforce":
            raise StaleFenceError("lease_expired")

        replay_key = (parent_task_id, attempt_id, stage_id)
        lease_key = (lease_id, fencing_token)
        with self._lock:
            self._ensure_healthy_locked()
            prev_digest = self._attempt_digests.get(replay_key)
            if prev_digest is not None:
                if prev_digest != request_digest:
                    raise ValueError("attempt_stage_conflict")
                for rid, irec in self._inflight_requests.items():
                    if irec.get("replay_key") == [parent_task_id, attempt_id, stage_id] and lease_id not in self._revoked_leases:
                        raise DuplicateInProgressError(rid)
                raise StaleFenceError("stale_or_revoked_fencing_token")
            active_owner = self._active_leases.get(lease_key)
            if active_owner is not None and active_owner != parent_task_id:
                raise ValueError("sibling_fencing_conflict")
            if (
                lease_id in self._revoked_leases
                or fencing_token in self._consumed_tokens
                or fencing_token <= self._min_fencing_token
            ):
                raise StaleFenceError("stale_or_revoked_fencing_token")
            next_min = max(self._min_fencing_token, fencing_token)
            next_digests = dict(self._attempt_digests)
            next_digests[replay_key] = request_digest
            next_inflight = dict(self._inflight_requests)
            next_inflight[request_id] = {
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "owner_pid": int(ident["supervisor_pid"]),
                "owner_ticks": int(ident["supervisor_starttime_ticks"]),
                "replay_key": [parent_task_id, attempt_id, stage_id],
            }
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=set(self._consumed_tokens),
                revoked_leases=set(self._revoked_leases),
                attempt_digests=next_digests,
                inflight_requests=next_inflight,
            )
            self._active_leases[lease_key] = parent_task_id
            return {
                "request_id": request_id,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "attempt_id": attempt_id,
                "stage_id": stage_id,
                "parent_task_id": parent_task_id,
                "pod_uid": pod_uid or local_pod,
                "container_id": container_id or local_cid,
                "boot_id": boot_id or local_boot,
                "gpu_uuid": gpu_uuid,
                "expires_epoch": expires_epoch,
            }

    def verify_grant_live(self, grant: dict[str, object] | None) -> None:
        with self._lock:
            self._ensure_healthy_locked()
            if grant is None:
                if self.policy == "enforce":
                    raise StaleFenceError("missing_or_invalid_gpu_grant")
                return
            lease_id = str(grant["lease_id"])
            token = int(grant["fencing_token"])
            expires_epoch = grant.get("expires_epoch")
            if lease_id in self._revoked_leases or token in self._consumed_tokens or token < self._min_fencing_token:
                raise StaleFenceError("stale_or_revoked_fencing_token")
            if isinstance(expires_epoch, (int, float)) and expires_epoch <= time.time() and self.policy == "enforce":
                raise StaleFenceError("lease_expired")

    def finish_grant(
        self,
        grant: dict[str, object] | None,
        *,
        request_id: str = "",
        active_tasks: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        req_id = request_id or (str(grant.get("request_id") or "") if isinstance(grant, dict) else "")
        with self._lock:
            if req_id:
                self._unheadered_requests.discard(req_id)
            if grant is None:
                if req_id and req_id in self._inflight_requests and not self._corrupted:
                    next_inflight = dict(self._inflight_requests)
                    next_inflight.pop(req_id, None)
                    self._commit_state_locked(
                        min_fencing_token=self._min_fencing_token,
                        consumed_tokens=set(self._consumed_tokens),
                        revoked_leases=set(self._revoked_leases),
                        attempt_digests=dict(self._attempt_digests),
                        inflight_requests=next_inflight,
                    )
                return None
            lease_id = str(grant["lease_id"])
            token = int(grant["fencing_token"])
            self._active_leases.pop((lease_id, token), None)
            if req_id and req_id in self._inflight_requests:
                next_inflight = dict(self._inflight_requests)
                next_inflight.pop(req_id, None)
            else:
                next_inflight = dict(self._inflight_requests)
            return self._reclaim_and_seal_locked(
                lease_id=lease_id,
                fencing_token=token,
                external_active=active_tasks,
                settled=settled,
                identity=identity,
                next_inflight_override=next_inflight,
            )

    def _reclaim_and_seal_locked(
        self,
        *,
        lease_id: str = "",
        fencing_token: int = 0,
        external_active: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
        next_inflight_override: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        self._ensure_healthy_locked()
        if not lease_id and fencing_token <= 0:
            return None
        ident = identity or read_runtime_identity()
        next_revoked = set(self._revoked_leases)
        next_consumed = set(self._consumed_tokens)
        next_min = self._min_fencing_token
        next_inflight = (
            dict(next_inflight_override)
            if next_inflight_override is not None
            else dict(self._inflight_requests)
        )
        if lease_id:
            next_revoked.add(lease_id)
        if fencing_token > 0:
            next_consumed.add(fencing_token)
            if fencing_token > next_min:
                next_min = fencing_token
        self._commit_state_locked(
            min_fencing_token=next_min,
            consumed_tokens=next_consumed,
            revoked_leases=next_revoked,
            attempt_digests=dict(self._attempt_digests),
            inflight_requests=next_inflight,
        )
        effective_active = max(
            int(external_active),
            len(self._inflight_requests),
            len(self._unheadered_requests),
        )
        consumed = fencing_token if fencing_token > 0 else self._min_fencing_token
        return {
            "reclaimed": bool(settled and effective_active == 0),
            "active_tasks": int(effective_active),
            "proof_source": "receiver_guard",
            "token_sealed": True,
            "supervisor_pid": int(ident["supervisor_pid"]),
            "supervisor_starttime_ticks": int(ident["supervisor_starttime_ticks"]),
            "sealed_pod_uid": str(ident["pod_uid"]),
            "sealed_container_id": str(ident["container_id"]),
            "sealed_boot_id": str(ident["boot_id"]),
            "revoked_lease_id": lease_id,
            "consumed_fencing_token": int(consumed),
        }

    def reclaim_and_seal(
        self,
        *,
        lease_id: str = "",
        fencing_token: int = 0,
        active_tasks: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            return self._reclaim_and_seal_locked(
                lease_id=lease_id,
                fencing_token=fencing_token,
                external_active=active_tasks,
                settled=settled,
                identity=identity,
            )

    def build_warm_reclaim_proof(
        self,
        *,
        lease_id: str = "",
        fencing_token: int = 0,
        active_tasks: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        return self.reclaim_and_seal(
            lease_id=lease_id,
            fencing_token=fencing_token,
            active_tasks=active_tasks,
            settled=settled,
            identity=identity,
        )

    def revoke_lease(self, lease_id: str, fencing_token: int) -> list[str]:
        affected: list[str] = []
        with self._lock:
            self._ensure_healthy_locked()
            next_revoked = set(self._revoked_leases)
            next_consumed = set(self._consumed_tokens)
            next_min = self._min_fencing_token
            if lease_id:
                next_revoked.add(lease_id)
            if fencing_token > 0:
                next_consumed.add(fencing_token)
                if fencing_token > next_min:
                    next_min = fencing_token
            for (lid, tok), owner in list(self._active_leases.items()):
                if lid == lease_id or tok <= fencing_token:
                    affected.append(owner)
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=next_consumed,
                revoked_leases=next_revoked,
                attempt_digests=dict(self._attempt_digests),
                inflight_requests=dict(self._inflight_requests),
            )
        return affected
