"""Receiver-side 6-tuple GPU grant validation, 2-card check, fail-closed durable fencing, upstream process attestation, and reclaim proof for MiniMax Music 3."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
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


def _win32_proc_starttime_ticks(pid: int) -> int | None:
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:  # STILL_ACTIVE
            return None
        ft_create = wintypes.FILETIME()
        ft_exit = wintypes.FILETIME()
        ft_kernel = wintypes.FILETIME()
        ft_user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(ft_create),
            ctypes.byref(ft_exit),
            ctypes.byref(ft_kernel),
            ctypes.byref(ft_user),
        ):
            return None
        ticks = (int(ft_create.dwHighDateTime) << 32) | int(ft_create.dwLowDateTime)
        return max(1, ticks)
    finally:
        kernel32.CloseHandle(handle)


def read_pid_starttime_ticks(pid: int, *, proc_root: str | Path = "/proc") -> int | None:
    if pid <= 0:
        return None
    raw_override = os.environ.get("VF_TEST_PROC_TICKS_JSON", "").strip()
    if raw_override:
        try:
            mapping = json.loads(raw_override)
            if isinstance(mapping, dict) and str(pid) in mapping:
                val = int(mapping[str(pid)])
                return val if val > 0 else None
        except (ValueError, TypeError):
            pass
    stat_file = Path(proc_root) / str(pid) / "stat"
    try:
        if stat_file.is_file():
            stat_raw = stat_file.read_text(encoding="utf-8")
            rparen = stat_raw.rfind(")")
            if rparen > 0:
                fields = stat_raw[rparen + 2 :].split()
                if len(fields) > 19 and fields[0] != "Z" and fields[19].isdigit():
                    ticks = int(fields[19])
                    if ticks > 0:
                        return ticks
            return None
    except OSError:
        return None
    if os.name == "nt":
        return _win32_proc_starttime_ticks(pid)
    return None


def discover_resident_procs(
    *,
    supervisor_pid: int,
    init_pid: int = 0,
    upstream_leader_pid: int = 0,
    upstream_pid_file: str | Path | None = None,
    proc_root: str | Path = "/proc",
) -> tuple[list[dict[str, int]], tuple[int, int] | None, bool]:
    eff_init = int(init_pid or int(os.environ.get("VF_CONTAINER_INIT_PID") or "0") or 0)
    eff_up = int(upstream_leader_pid or 0)
    pid_file_raw = (
        str(upstream_pid_file).strip()
        if upstream_pid_file is not None
        else os.environ.get("VF_UPSTREAM_PID_FILE", "").strip()
    )
    if pid_file_raw:
        try:
            pf = Path(pid_file_raw)
            if pf.is_file():
                txt = pf.read_text(encoding="utf-8").strip()
                if txt.isdigit() and int(txt) > 0:
                    eff_up = int(txt)
        except OSError:
            pass
    if eff_up <= 0:
        env_up = os.environ.get("VF_UPSTREAM_LEADER_PID", "").strip()
        if env_up.isdigit() and int(env_up) > 0:
            eff_up = int(env_up)

    upstream_configured = eff_up > 0
    up_ticks = read_pid_starttime_ticks(eff_up, proc_root=proc_root) if eff_up > 0 else None
    upstream_ident = (eff_up, up_ticks) if (eff_up > 0 and up_ticks is not None) else None

    resident_map: dict[int, int] = {}
    if eff_init > 0 and eff_init != supervisor_pid:
        init_ticks = read_pid_starttime_ticks(eff_init, proc_root=proc_root)
        if init_ticks is not None:
            resident_map[eff_init] = init_ticks
    if upstream_ident is not None and eff_up != supervisor_pid:
        resident_map[eff_up] = up_ticks  # type: ignore[assignment]

    proc_dir = Path(proc_root)
    if proc_dir.is_dir():
        children_by_ppid: dict[int, list[int]] = {}
        ticks_by_pid: dict[int, int] = {}
        try:
            entries = list(proc_dir.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            p = int(entry.name)
            try:
                sraw = (entry / "stat").read_text(encoding="utf-8")
            except OSError:
                continue
            rparen = sraw.rfind(")")
            if rparen <= 0:
                continue
            fields = sraw[rparen + 2 :].split()
            if len(fields) <= 19 or fields[0] == "Z" or not fields[1].isdigit() or not fields[19].isdigit():
                continue
            ppid = int(fields[1])
            pticks = int(fields[19])
            if pticks <= 0:
                continue
            ticks_by_pid[p] = pticks
            children_by_ppid.setdefault(ppid, []).append(p)

        seeds: list[int] = []
        if upstream_ident is not None:
            seeds.append(eff_up)
        if supervisor_pid > 0:
            seeds.extend(children_by_ppid.get(supervisor_pid, []))
        visited: set[int] = set()
        queue = list(seeds)
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            if cur != supervisor_pid and cur in ticks_by_pid:
                resident_map[cur] = ticks_by_pid[cur]
            for child in children_by_ppid.get(cur, []):
                if child not in visited and child != supervisor_pid:
                    queue.append(child)

    resident_procs = [{"pid": pid, "starttime_ticks": resident_map[pid]} for pid in sorted(resident_map)]
    return resident_procs, upstream_ident, upstream_configured


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
    if os.name == "nt":
        wt = _win32_proc_starttime_ticks(os.getpid())
        if wt is not None:
            return wt
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
        min_cards: int = 2,
        state_path: str | Path | None = None,
        init_pid: int = 0,
        upstream_leader_pid: int = 0,
        upstream_pid_file: str | Path | None = None,
        proc_root: str | Path = "/proc",
        write_fn=None,
        fsync_fn=None,
        replace_fn=None,
        fsync_dir_fn=None,
    ) -> None:
        self._policy_env = policy_env
        self._min_cards = max(1, int(min_cards))
        raw_state = str(state_path).strip() if state_path is not None else os.environ.get("VF_GPU_GUARD_STATE_PATH", "").strip()
        self._state_path = Path(raw_state) if raw_state else None
        self._init_pid = int(init_pid or 0)
        self._upstream_leader_pid = int(upstream_leader_pid or 0)
        self._upstream_pid_file = upstream_pid_file
        self._proc_root = proc_root
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
        self._replay_records: dict[tuple[str, str, str], dict[str, object]] = {}
        self._inflight_records: dict[str, dict[str, object]] = {}
        self._load_state_locked()

    def _ensure_healthy_locked(self) -> None:
        if self._corrupted:
            raise FencePersistenceError(self._corrupt_reason or "fence_state_corrupted")

    def inspect_topology(self, *, supervisor_pid: int | None = None) -> tuple[list[dict[str, int]], tuple[int, int] | None, bool]:
        sup_pid = int(supervisor_pid if supervisor_pid is not None else os.getpid())
        return discover_resident_procs(
            supervisor_pid=sup_pid,
            init_pid=self._init_pid,
            upstream_leader_pid=self._upstream_leader_pid,
            upstream_pid_file=self._upstream_pid_file,
            proc_root=self._proc_root,
        )

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
            raw_replays = payload.get("replay_records", {})
            if not isinstance(raw_replays, dict):
                raise ValueError("invalid_replay_records")
            replays: dict[tuple[str, str, str], dict[str, object]] = {}
            for k, v in raw_replays.items():
                parts = str(k).split("\x1f")
                if len(parts) != 3 or not isinstance(v, dict) or not isinstance(v.get("digest"), str):
                    raise ValueError("invalid_replay_record_entry")
                replays[(parts[0], parts[1], parts[2])] = dict(v)
            raw_inflight = payload.get("inflight_records", {})
            if not isinstance(raw_inflight, dict):
                raise ValueError("invalid_inflight_records")

            _, upstream_ident, _ = self.inspect_topology()
            inflight: dict[str, dict[str, object]] = {}
            needs_repersist = False
            for rid, meta in raw_inflight.items():
                if not isinstance(rid, str) or not isinstance(meta, dict):
                    raise ValueError("invalid_inflight_record_entry")
                rec_up_pid = int(meta.get("upstream_pid") or 0)
                rec_up_ticks = int(meta.get("upstream_ticks") or 0)
                lid = str(meta.get("lease_id") or "").strip()
                tok = int(meta.get("fencing_token") or 0)

                if rec_up_pid > 0 and upstream_ident != (rec_up_pid, rec_up_ticks):
                    # Upstream sgl-omni exited or restarted with new (pid, ticks) -> provably no surviving GPU work
                    if lid:
                        revoked.add(lid)
                    if tok > 0:
                        consumed.add(tok)
                        if tok > raw_min:
                            raw_min = tok
                    rk_raw = meta.get("replay_key")
                    if isinstance(rk_raw, list) and len(rk_raw) == 3:
                        rk = (str(rk_raw[0]), str(rk_raw[1]), str(rk_raw[2]))
                        if rk in replays and replays[rk].get("state") == "running":
                            replays[rk]["state"] = "uncertain"
                    needs_repersist = True
                    continue

                # Same upstream sgl-omni is still running -> keep record as uncertain so /reclaim returns 409!
                updated = dict(meta)
                if updated.get("phase") != "uncertain":
                    updated["phase"] = "uncertain"
                    needs_repersist = True
                rk_raw = meta.get("replay_key")
                if isinstance(rk_raw, list) and len(rk_raw) == 3:
                    rk = (str(rk_raw[0]), str(rk_raw[1]), str(rk_raw[2]))
                    if rk in replays and replays[rk].get("state") == "running":
                        replays[rk]["state"] = "uncertain"
                        needs_repersist = True
                if lid and lid not in revoked:
                    revoked.add(lid)
                    needs_repersist = True
                if tok > 0 and tok not in consumed:
                    consumed.add(tok)
                    if tok > raw_min:
                        raw_min = tok
                    needs_repersist = True
                inflight[rid] = updated

            for _rk, rmeta in replays.items():
                if rmeta.get("state") == "running":
                    rmeta["state"] = "uncertain"
                    needs_repersist = True

            self._min_fencing_token = raw_min
            self._consumed_tokens = consumed
            self._revoked_leases = revoked
            self._attempt_digests = digests
            self._replay_records = replays
            self._inflight_records = inflight
            if needs_repersist:
                self._commit_state_locked(
                    min_fencing_token=raw_min,
                    consumed_tokens=consumed,
                    revoked_leases=revoked,
                    attempt_digests=digests,
                    inflight_records=inflight,
                    replay_records=replays,
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
        inflight_records: dict[str, dict[str, object]],
        replay_records: dict[tuple[str, str, str], dict[str, object]] | None = None,
    ) -> None:
        self._ensure_healthy_locked()
        effective_replays = (
            dict(replay_records) if replay_records is not None else dict(self._replay_records)
        )
        if self._state_path is not None:
            tmp_path = self._state_path.with_name(
                f"{self._state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                serialized_digests = {f"{p}\x1f{a}\x1f{s}": d for (p, a, s), d in attempt_digests.items()}
                serialized_replays = {f"{p}\x1f{a}\x1f{s}": dict(v) for (p, a, s), v in effective_replays.items()}
                data = json.dumps(
                    {
                        "min_fencing_token": min_fencing_token,
                        "consumed_tokens": sorted(consumed_tokens),
                        "revoked_leases": sorted(revoked_leases),
                        "attempt_digests": serialized_digests,
                        "replay_records": serialized_replays,
                        "inflight_records": inflight_records,
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
        self._replay_records = effective_replays
        self._inflight_records = inflight_records

    def _write_cached_response_blob_locked(self, request_id: str, body: bytes) -> tuple[str | None, str | None]:
        if len(body) <= 65536 or self._state_path is None:
            return base64.b64encode(body).decode("ascii"), None
        blob_path = self._state_path.with_name(f"{self._state_path.name}.resp.{request_id}.bin")
        tmp_blob = blob_path.with_name(f"{blob_path.name}.tmp")
        fd = os.open(str(tmp_blob), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(body)
            while view:
                written = self._write_fn(fd, view)
                if written <= 0:
                    raise OSError("short_write_zero_bytes")
                view = view[written:]
            self._fsync_fn(fd)
        finally:
            os.close(fd)
        self._replace_fn(str(tmp_blob), str(blob_path))
        self._fsync_dir_fn(self._state_path.parent)
        try:
            prefix = f"{self._state_path.name}.resp."
            siblings = sorted(
                [p for p in self._state_path.parent.iterdir() if p.name.startswith(prefix) and p.name.endswith(".bin")],
                key=lambda p: p.stat().st_mtime,
            )
            while len(siblings) > 8:
                oldest = siblings.pop(0)
                if oldest != blob_path:
                    oldest.unlink(missing_ok=True)
        except OSError:
            pass
        return None, blob_path.name

    def _read_cached_response_bytes_locked(self, rmeta: dict[str, object]) -> bytes | None:
        b64 = rmeta.get("response_body_b64")
        if isinstance(b64, str):
            try:
                return base64.b64decode(b64.encode("ascii"))
            except ValueError:
                return None
        blob_name = rmeta.get("response_blob_name")
        if isinstance(blob_name, str) and blob_name and self._state_path is not None:
            blob_path = self._state_path.parent / blob_name
            try:
                if blob_path.is_file():
                    return blob_path.read_bytes()
            except OSError:
                return None
        return None

    def _prune_dead_upstream_locked(self, upstream_ident: tuple[int, int] | None) -> None:
        if not self._inflight_records:
            return
        next_inflight = dict(self._inflight_records)
        next_revoked = set(self._revoked_leases)
        next_consumed = set(self._consumed_tokens)
        next_min = self._min_fencing_token
        changed = False
        for rid, meta in list(next_inflight.items()):
            rec_pid = int(meta.get("upstream_pid") or 0)
            rec_ticks = int(meta.get("upstream_ticks") or 0)
            if rec_pid > 0 and upstream_ident != (rec_pid, rec_ticks):
                next_inflight.pop(rid, None)
                lid = str(meta.get("lease_id") or "").strip()
                tok = int(meta.get("fencing_token") or 0)
                if lid:
                    next_revoked.add(lid)
                if tok > 0:
                    next_consumed.add(tok)
                    if tok > next_min:
                        next_min = tok
                self._active_leases.pop((lid, tok), None)
                changed = True
        if changed:
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=next_consumed,
                revoked_leases=next_revoked,
                attempt_digests=dict(self._attempt_digests),
                inflight_records=next_inflight,
            )

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

    def active_count(self) -> int:
        with self._lock:
            if not self._corrupted:
                _, upstream_ident, _ = self.inspect_topology()
                self._prune_dead_upstream_locked(upstream_ident)
            return len(self._inflight_records)

    def begin_proxied_request(
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
        _, upstream_ident, upstream_configured = self.inspect_topology(supervisor_pid=int(ident["supervisor_pid"]))
        if upstream_configured and upstream_ident is None:
            raise FencePersistenceError("upstream_engine_not_running")
        up_pid = upstream_ident[0] if upstream_ident is not None else 0
        up_ticks = upstream_ident[1] if upstream_ident is not None else 0

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
        if not has_any or not (lease_id and token_raw and attempt_id and stage_id):
            if policy == "enforce":
                raise PermissionError("missing_or_invalid_gpu_grant")
            if policy == "warn" and not has_any:
                logging.warning("music3_gpu_grant_missing_warn request_id=%s", request_id)
            with self._lock:
                self._ensure_healthy_locked()
                self._prune_dead_upstream_locked(upstream_ident)
                next_inflight = dict(self._inflight_records)
                next_inflight[request_id] = {
                    "lease_id": "",
                    "fencing_token": 0,
                    "upstream_pid": up_pid,
                    "upstream_ticks": up_ticks,
                    "phase": "running",
                }
                self._commit_state_locked(
                    min_fencing_token=self._min_fencing_token,
                    consumed_tokens=set(self._consumed_tokens),
                    revoked_leases=set(self._revoked_leases),
                    attempt_digests=dict(self._attempt_digests),
                    inflight_records=next_inflight,
                )
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
        supplied_gpus = [item.strip() for item in gpu_uuid.split(",") if item.strip()]
        if policy == "enforce":
            if (
                is_unknown_identity_value(local_pod)
                or is_unknown_identity_value(local_cid)
                or is_unknown_identity_value(local_boot)
                or len(visible) < self._min_cards
            ):
                raise PermissionError("gpu_identity_unavailable")
            if not all((pod_uid, container_id, boot_id, gpu_uuid, expires_raw)):
                raise PermissionError("missing_or_invalid_gpu_grant")
            if len(set(supplied_gpus)) < self._min_cards:
                raise PermissionError("insufficient_granted_gpu_cards")

        if pod_uid and not is_unknown_identity_value(local_pod) and pod_uid != local_pod:
            raise PermissionError("pod_uid_mismatch")
        if container_id and not is_unknown_identity_value(local_cid):
            if normalize_container_id(container_id) != normalize_container_id(local_cid):
                raise PermissionError("container_id_mismatch")
        if boot_id and not is_unknown_identity_value(local_boot) and boot_id != local_boot:
            raise PermissionError("boot_id_mismatch")
        if supplied_gpus and visible:
            for g in supplied_gpus:
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
            self._prune_dead_upstream_locked(upstream_ident)
            existing_replay = self._replay_records.get(replay_key)
            prev_digest = self._attempt_digests.get(replay_key)
            if existing_replay is not None:
                if existing_replay.get("digest") != request_digest:
                    raise ValueError("attempt_stage_conflict")
                r_state = str(existing_replay.get("state") or "")
                if r_state == "running" and not existing_replay.get("explicitly_revoked"):
                    return {
                        "dedup_action": "in_progress",
                        "request_id": str(existing_replay.get("request_id") or ""),
                        "lease_id": lease_id,
                        "fencing_token": fencing_token,
                    }
                if r_state == "completed" and not existing_replay.get("explicitly_revoked"):
                    cached_bytes = self._read_cached_response_bytes_locked(existing_replay)
                    if cached_bytes is not None:
                        return {
                            "dedup_action": "cached_response",
                            "request_id": str(existing_replay.get("request_id") or ""),
                            "response_status": int(existing_replay.get("response_status") or 200),
                            "response_bytes": cached_bytes,
                            "content_type": str(existing_replay.get("content_type") or "application/octet-stream"),
                            "reclaim_proof": existing_replay.get("reclaim_proof"),
                            "lease_id": lease_id,
                            "fencing_token": fencing_token,
                        }
                raise StaleFenceError("replay_uncertain_or_sealed")
            if prev_digest is not None:
                if prev_digest != request_digest:
                    raise ValueError("attempt_stage_conflict")
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
            next_replays = dict(self._replay_records)
            next_replays[replay_key] = {
                "digest": request_digest,
                "request_id": request_id,
                "state": "running",
                "response_status": None,
                "response_body_b64": None,
                "response_blob_name": None,
                "content_type": "application/octet-stream",
                "reclaim_proof": None,
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "explicitly_revoked": False,
            }
            next_inflight = dict(self._inflight_records)
            next_inflight[request_id] = {
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "upstream_pid": up_pid,
                "upstream_ticks": up_ticks,
                "phase": "running",
                "replay_key": [parent_task_id, attempt_id, stage_id],
            }
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=set(self._consumed_tokens),
                revoked_leases=set(self._revoked_leases),
                attempt_digests=next_digests,
                inflight_records=next_inflight,
                replay_records=next_replays,
            )
            self._active_leases[lease_key] = parent_task_id
            return {
                "dedup_action": "forward",
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

    def mark_inflight_uncertain(self, request_id: str) -> None:
        with self._lock:
            if self._corrupted:
                return
            rec = self._inflight_records.get(request_id)
            if rec is None:
                return
            next_inflight = dict(self._inflight_records)
            updated = dict(rec)
            updated["phase"] = "uncertain"
            next_inflight[request_id] = updated
            next_replays = dict(self._replay_records)
            rk_raw = rec.get("replay_key")
            if isinstance(rk_raw, list) and len(rk_raw) == 3:
                rk = (str(rk_raw[0]), str(rk_raw[1]), str(rk_raw[2]))
                if rk in next_replays:
                    rep_upd = dict(next_replays[rk])
                    rep_upd["state"] = "uncertain"
                    next_replays[rk] = rep_upd
            next_revoked = set(self._revoked_leases)
            next_consumed = set(self._consumed_tokens)
            next_min = self._min_fencing_token
            lid = str(rec.get("lease_id") or "").strip()
            tok = int(rec.get("fencing_token") or 0)
            if lid:
                next_revoked.add(lid)
            if tok > 0:
                next_consumed.add(tok)
                if tok > next_min:
                    next_min = tok
            self._active_leases.pop((lid, tok), None)
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=next_consumed,
                revoked_leases=next_revoked,
                attempt_digests=dict(self._attempt_digests),
                inflight_records=next_inflight,
                replay_records=next_replays,
            )

    def complete_proxied_request(
        self,
        request_id: str,
        *,
        settled: bool = True,
        response_status: int | None = None,
        response_body: bytes | None = None,
        content_type: str | None = None,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            self._ensure_healthy_locked()
            rec = self._inflight_records.get(request_id)
            if rec is None:
                return None
            lid = str(rec.get("lease_id") or "").strip()
            tok = int(rec.get("fencing_token") or 0)
            self._active_leases.pop((lid, tok), None)
            next_inflight = dict(self._inflight_records)
            next_inflight.pop(request_id, None)
            rk_raw = rec.get("replay_key")
            rk_tuple: tuple[str, str, str] | None = None
            if isinstance(rk_raw, list) and len(rk_raw) == 3:
                rk_tuple = (str(rk_raw[0]), str(rk_raw[1]), str(rk_raw[2]))
            if not lid and tok <= 0:
                self._commit_state_locked(
                    min_fencing_token=self._min_fencing_token,
                    consumed_tokens=set(self._consumed_tokens),
                    revoked_leases=set(self._revoked_leases),
                    attempt_digests=dict(self._attempt_digests),
                    inflight_records=next_inflight,
                )
                return None
            proof = self._reclaim_and_seal_locked(
                lease_id=lid,
                fencing_token=tok,
                settled=settled,
                identity=identity,
                next_inflight_override=next_inflight,
            )
            if rk_tuple is not None and rk_tuple in self._replay_records:
                next_replays = dict(self._replay_records)
                rep_upd = dict(next_replays[rk_tuple])
                if response_status is not None and 200 <= int(response_status) < 300 and response_body is not None:
                    b64, blob_name = self._write_cached_response_blob_locked(request_id, response_body)
                    rep_upd["state"] = "completed"
                    rep_upd["response_status"] = int(response_status)
                    rep_upd["response_body_b64"] = b64
                    rep_upd["response_blob_name"] = blob_name
                    rep_upd["content_type"] = str(content_type or "application/octet-stream")
                    rep_upd["reclaim_proof"] = proof
                else:
                    rep_upd["state"] = "aborted"
                next_replays[rk_tuple] = rep_upd
                self._commit_state_locked(
                    min_fencing_token=self._min_fencing_token,
                    consumed_tokens=set(self._consumed_tokens),
                    revoked_leases=set(self._revoked_leases),
                    attempt_digests=dict(self._attempt_digests),
                    inflight_records=dict(self._inflight_records),
                    replay_records=next_replays,
                )
            return proof

    def _reclaim_and_seal_locked(
        self,
        *,
        lease_id: str = "",
        fencing_token: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
        next_inflight_override: dict[str, dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        self._ensure_healthy_locked()
        if not lease_id and fencing_token <= 0:
            return None
        ident = identity or read_runtime_identity()
        resident_procs, upstream_ident, upstream_configured = self.inspect_topology(
            supervisor_pid=int(ident["supervisor_pid"])
        )
        if next_inflight_override is None:
            self._prune_dead_upstream_locked(upstream_ident)
            next_inflight = dict(self._inflight_records)
        else:
            next_inflight = dict(next_inflight_override)

        next_revoked = set(self._revoked_leases)
        next_consumed = set(self._consumed_tokens)
        next_min = self._min_fencing_token
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
            inflight_records=next_inflight,
        )

        upstream_healthy = (not upstream_configured) or (upstream_ident is not None)
        procs_within_cap = len(resident_procs) <= 32
        effective_active = len(self._inflight_records)
        is_reclaimed = bool(settled and effective_active == 0 and upstream_healthy and procs_within_cap)
        consumed = fencing_token if fencing_token > 0 else self._min_fencing_token
        proof: dict[str, object] = {
            "reclaimed": is_reclaimed,
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
        if resident_procs:
            proof["resident_procs"] = resident_procs
        if upstream_configured:
            proof["upstream_alive"] = upstream_ident is not None
        return proof

    def reclaim_and_seal(
        self,
        *,
        lease_id: str = "",
        fencing_token: int = 0,
        settled: bool = True,
        identity: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            return self._reclaim_and_seal_locked(
                lease_id=lease_id,
                fencing_token=fencing_token,
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
            settled=bool(settled and int(active_tasks) == 0),
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
            next_replays = dict(self._replay_records)
            for (rk_p, rk_a, rk_s), rmeta in list(next_replays.items()):
                rlid = str(rmeta.get("lease_id") or "")
                rtok = int(rmeta.get("fencing_token") or 0)
                if (lease_id and rlid == lease_id) or (fencing_token > 0 and rtok <= fencing_token):
                    upd = dict(rmeta)
                    upd["explicitly_revoked"] = True
                    next_replays[(rk_p, rk_a, rk_s)] = upd
            for (lid, tok), owner in list(self._active_leases.items()):
                if lid == lease_id or tok <= fencing_token:
                    affected.append(owner)
            self._commit_state_locked(
                min_fencing_token=next_min,
                consumed_tokens=next_consumed,
                revoked_leases=next_revoked,
                attempt_digests=dict(self._attempt_digests),
                inflight_records=dict(self._inflight_records),
                replay_records=next_replays,
            )
        return affected
