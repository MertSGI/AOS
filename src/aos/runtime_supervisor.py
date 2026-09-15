"""Detached supervisor for AOS Runtime V1 candidate/stable slots."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_slots import SlotManager, SlotRecord
from aos.runtime_store import atomic_json, read_json


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "DETACHED_PROCESS", 0)) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


class SupervisorAlreadyRunning(RuntimeError):
    pass


class SupervisorSingleton:
    """Cross-process singleton guard for Runtime V1 supervisor ownership."""

    def __init__(self, root: Path, name: str = r"Local\AOS.RuntimeV1.Supervisor") -> None:
        self.root = root.expanduser().resolve()
        self.name = name
        self._handle = None
        self._file = None

    def __enter__(self) -> "SupervisorSingleton":
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            create_mutex.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            handle = create_mutex(None, False, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:
                close_handle(handle)
                raise SupervisorAlreadyRunning(f"Runtime supervisor singleton already held: {self.name}")
            self._handle = (kernel32, handle)
            return self
        import fcntl
        lock_path = self.root / "supervisor.lock"
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SupervisorAlreadyRunning(f"Runtime supervisor singleton already held: {lock_path}")
        handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush()
        self._file = handle
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            try:
                kernel32.CloseHandle(handle)
            finally:
                self._handle = None
        if self._file is not None:
            try:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close(); self._file = None


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {value}", "/FO", "CSV", "/NH"],
                text=True, capture_output=True, timeout=10,
            )
            return proc.returncode == 0 and str(value) in (proc.stdout or "")
        os.kill(value, 0)
        return True
    except Exception:
        return False


def _http_health(url: str) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "AOS-Supervisor/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if int(resp.status) != 200:
                return None
            value = json.loads(resp.read().decode("utf-8"))
            if not isinstance(value, dict):
                return None
            if value.get("contract_version") != CONTRACT_VERSION or value.get("runtime_state") != "HEALTHY":
                return None
            return value
    except Exception:
        return None


def _runtime_health_matches(
    slot: SlotRecord, health: Optional[Dict[str, Any]], launch_nonce: Optional[str], supervisor_pid: Any
) -> bool:
    """Bind Runtime V1 health to the owning supervisor launch identity.

    On Windows a venv ``pythonw.exe`` can be a redirector: the PID returned by
    ``Popen`` is then only a short-lived launcher PID, while the actual API
    process has a different PID. Process ownership therefore uses the
    supervisor PID + cryptographic launch nonce + exact slot/SHA, not launcher
    PID equality.
    """
    if not isinstance(health, dict):
        return False
    try:
        observed_pid = int(health.get("pid"))
        observed_supervisor = int(health.get("runtime_supervisor_pid"))
        expected_supervisor = int(supervisor_pid)
    except (TypeError, ValueError):
        return False
    if observed_pid <= 0 or observed_supervisor != expected_supervisor:
        return False
    if slot.source_sha and health.get("runtime_source_sha") != slot.source_sha:
        return False
    if health.get("runtime_slot_id") != slot.slot_id:
        return False
    if not launch_nonce or health.get("runtime_launch_nonce") != launch_nonce:
        return False
    return True



def _terminate_pid(pid: Any) -> None:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return
    if value <= 0 or value == os.getpid():
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(value), "/F"],
                text=True, capture_output=True, timeout=15,
            )
        else:
            os.kill(value, 15)
    except Exception:
        return


def _verified_runtime_identity(slot: SlotRecord, health: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(health, dict):
        return False
    try:
        if int(health.get("pid")) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    if slot.source_sha and health.get("runtime_source_sha") != slot.source_sha:
        return False
    return health.get("runtime_slot_id") == slot.slot_id



class RuntimeSupervisor:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser().resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.root = Path(self.config["supervisor_root"]).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots = SlotManager(self.root)
        self.state_path = self.root / "supervisor-state.json"
        self.child: Optional[subprocess.Popen] = None
        self.child_slot_id: Optional[str] = None
        self.failures = 0
        self.last_health: Optional[Dict[str, Any]] = None
        self.launch_nonce: Optional[str] = None
        self.runtime_api_pid: Optional[int] = None
        self.singleton_name = str(self.config.get("singleton_name") or r"Local\AOS.RuntimeV1.Supervisor")

    def _write_state(self, **updates: Any) -> Dict[str, Any]:
        state = read_json(self.state_path, {})
        state.update(updates)
        state.update({
            "contract_version": CONTRACT_VERSION,
            "updated_at": utc_now(),
            "supervisor_pid": os.getpid(),
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })
        atomic_json(self.state_path, state)
        return state

    def _stop_owned_runtime(self) -> None:
        """Stop only the API process proven to belong to this supervisor launch."""
        health = self.last_health
        if isinstance(health, dict):
            try:
                owner = int(health.get("runtime_supervisor_pid"))
            except (TypeError, ValueError):
                owner = -1
            if (
                owner == os.getpid()
                and self.launch_nonce
                and health.get("runtime_launch_nonce") == self.launch_nonce
                and self.child_slot_id
            ):
                try:
                    slot = self.slots.read_slot(self.child_slot_id)
                except Exception:
                    slot = None
                if slot is not None and _verified_runtime_identity(slot, health):
                    _terminate_pid(health.get("pid"))
        self.runtime_api_pid = None
        self.last_health = None

    def _reclaim_orphan_runtime(self, slot: SlotRecord) -> None:
        """Reclaim an exact-slot API left behind by a dead former supervisor.

        Never kill a process merely because it owns the port. The endpoint must
        prove the exact slot/SHA and identify a supervisor PID that is no longer
        alive.
        """
        health = _http_health(slot.health_url) if slot.health_url else None
        if not _verified_runtime_identity(slot, health):
            return
        try:
            owner = int(health.get("runtime_supervisor_pid"))
            api_pid = int(health.get("pid"))
        except (TypeError, ValueError):
            return
        if owner == os.getpid():
            return
        if pid_alive(owner):
            # A live different owner should be impossible while our singleton is
            # held. Fail closed and let health/activation expose the conflict.
            return
        _terminate_pid(api_pid)
        deadline = time.time() + 10
        while time.time() < deadline:
            if not pid_alive(api_pid):
                break
            time.sleep(0.2)

    def _launch(self, slot: SlotRecord) -> None:
        # A healthy API already bound to this exact launch is authoritative even
        # if the Windows venv redirector PID returned by Popen has exited.
        if self.child_slot_id == slot.slot_id and _runtime_health_matches(
            slot, self.last_health, self.launch_nonce, os.getpid()
        ):
            return

        if self.child_slot_id and self.child_slot_id != slot.slot_id:
            self._stop_owned_runtime()
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.terminate()
                self.child.wait(timeout=10)
            except Exception:
                try:
                    self.child.kill()
                except Exception:
                    pass

        self._reclaim_orphan_runtime(slot)
        self.launch_nonce = secrets.token_hex(24)
        child_env = dict(os.environ)
        child_env.update({
            "AOS_RUNTIME_LAUNCH_NONCE": self.launch_nonce,
            "AOS_RUNTIME_SLOT_ID": slot.slot_id,
            "AOS_RUNTIME_SOURCE_SHA": slot.source_sha or "",
            "AOS_RUNTIME_SUPERVISOR_PID": str(os.getpid()),
            "AG_BACKEND_ENABLED": "FALSE",
        })
        self.child = subprocess.Popen(
            list(slot.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_creationflags(),
            env=child_env,
        )
        self.child_slot_id = slot.slot_id
        self.runtime_api_pid = None
        self.last_health = None
        self._write_state(
            state="STARTING",
            active_slot=slot.slot_id,
            active_kind=slot.kind,
            child_pid=self.child.pid,  # backward-compatible launcher PID field
            launcher_pid=self.child.pid,
            runtime_api_pid=None,
            source_sha=slot.source_sha,
            launch_nonce=self.launch_nonce,
            singleton_name=self.singleton_name,
            singleton_held=True,
        )

    def _healthy(self, slot: SlotRecord) -> bool:
        if slot.kind == "runtime_v1":
            self.last_health = _http_health(slot.health_url) if slot.health_url else None
            matched = _runtime_health_matches(slot, self.last_health, self.launch_nonce, os.getpid())
            if matched:
                try:
                    self.runtime_api_pid = int(self.last_health.get("pid"))
                except (TypeError, ValueError):
                    self.runtime_api_pid = None
            return matched
        # Legacy fallback has no Runtime V1 API. Its acceptance here is limited to
        # process liveness; stable promotion is never inferred from this.
        self.last_health = None
        return self.child is not None and self.child.poll() is None

    def run(self) -> int:
        with SupervisorSingleton(self.root, self.singleton_name):
            self._write_state(
                state="SUPERVISOR_STARTED",
                singleton_name=self.singleton_name,
                singleton_held=True,
            )
            return self._run_owned_loop()

    def _run_owned_loop(self) -> int:
        while True:
            pointer = self.slots.read_pointer()
            slot = self.slots.active_slot()
            self._launch(slot)
            grace = int(self.config.get("health_grace_seconds", 20))
            deadline = time.time() + max(5, grace)
            while time.time() < deadline:
                if self._healthy(slot):
                    break
                if slot.kind != "runtime_v1" and (self.child is None or self.child.poll() is not None):
                    break
                time.sleep(1)

            if self._healthy(slot):
                self.failures = 0
                if slot.kind == "runtime_v1" and pointer.get("active") == "candidate":
                    self.slots.mark_candidate_healthy()
                observed = self.last_health or {}
                self._write_state(
                    state="HEALTHY",
                    active_slot=slot.slot_id,
                    child_pid=self.child.pid if self.child else None,
                    launcher_pid=self.child.pid if self.child else None,
                    runtime_api_pid=observed.get("pid"),
                    observed_api_pid=observed.get("pid"),
                    observed_runtime_supervisor_pid=observed.get("runtime_supervisor_pid"),
                    observed_runtime_source_sha=observed.get("runtime_source_sha"),
                    observed_runtime_slot_id=observed.get("runtime_slot_id"),
                    observed_launch_nonce=observed.get("runtime_launch_nonce"),
                    launch_nonce=self.launch_nonce,
                    singleton_held=True,
                )
                time.sleep(max(2, int(self.config.get("poll_seconds", 5))))
                continue

            self.failures += 1
            observed = self.last_health or {}
            self._write_state(
                state="UNHEALTHY",
                active_slot=slot.slot_id,
                consecutive_failures=self.failures,
                child_pid=self.child.pid if self.child else None,
                launcher_pid=self.child.pid if self.child else None,
                runtime_api_pid=observed.get("pid"),
                observed_api_pid=observed.get("pid"),
                observed_runtime_supervisor_pid=observed.get("runtime_supervisor_pid"),
                observed_runtime_source_sha=observed.get("runtime_source_sha"),
                observed_runtime_slot_id=observed.get("runtime_slot_id"),
                observed_launch_nonce=observed.get("runtime_launch_nonce"),
                launch_nonce=self.launch_nonce,
                singleton_held=True,
            )
            max_failures = int(self.config.get("candidate_restart_limit", 3))
            if pointer.get("active") == "candidate" and self.failures >= max_failures:
                self._stop_owned_runtime()
                self.slots.rollback(reason=f"candidate failed health {self.failures} consecutive times")
                self._write_state(state="ROLLED_BACK_TO_STABLE", rollback_from=slot.slot_id)
                self.failures = 0
                continue
            time.sleep(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Runtime V1 candidate/stable supervisor")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return RuntimeSupervisor(Path(args.config)).run()
    except SupervisorAlreadyRunning:
        return 17
    except KeyboardInterrupt:
        return 130
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
