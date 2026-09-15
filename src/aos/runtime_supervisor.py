"""Detached supervisor for AOS Runtime V1 candidate/stable slots."""
from __future__ import annotations

import argparse
import json
import os
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


def _runtime_health_matches(slot: SlotRecord, child_pid: Any, health: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(health, dict):
        return False
    try:
        observed_pid = int(health.get("pid"))
        expected_pid = int(child_pid)
    except (TypeError, ValueError):
        return False
    if observed_pid != expected_pid:
        return False
    if slot.source_sha and health.get("runtime_source_sha") != slot.source_sha:
        return False
    if health.get("runtime_slot_id") != slot.slot_id:
        return False
    return True


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

    def _launch(self, slot: SlotRecord) -> None:
        if self.child is not None and self.child.poll() is None and self.child_slot_id == slot.slot_id:
            return
        if self.child is not None and self.child.poll() is None:
            try:
                self.child.terminate()
                self.child.wait(timeout=10)
            except Exception:
                try:
                    self.child.kill()
                except Exception:
                    pass
        self.child = subprocess.Popen(
            list(slot.command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_creationflags(),
        )
        self.child_slot_id = slot.slot_id
        self._write_state(
            state="STARTING",
            active_slot=slot.slot_id,
            active_kind=slot.kind,
            child_pid=self.child.pid,
            source_sha=slot.source_sha,
        )

    def _healthy(self, slot: SlotRecord) -> bool:
        if self.child is None or self.child.poll() is not None:
            self.last_health = None
            return False
        if slot.kind == "runtime_v1":
            self.last_health = _http_health(slot.health_url) if slot.health_url else None
            return _runtime_health_matches(slot, self.child.pid, self.last_health)
        # Legacy fallback has no Runtime V1 API. Its acceptance here is limited to
        # process liveness; stable promotion is never inferred from this.
        self.last_health = None
        return True

    def run(self) -> int:
        self._write_state(state="SUPERVISOR_STARTED")
        while True:
            pointer = self.slots.read_pointer()
            slot = self.slots.active_slot()
            self._launch(slot)
            grace = int(self.config.get("health_grace_seconds", 20))
            deadline = time.time() + max(5, grace)
            while time.time() < deadline:
                if self._healthy(slot):
                    break
                if self.child is None or self.child.poll() is not None:
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
                    observed_api_pid=observed.get("pid"),
                    observed_runtime_source_sha=observed.get("runtime_source_sha"),
                    observed_runtime_slot_id=observed.get("runtime_slot_id"),
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
                observed_api_pid=observed.get("pid"),
                observed_runtime_source_sha=observed.get("runtime_source_sha"),
                observed_runtime_slot_id=observed.get("runtime_slot_id"),
            )
            max_failures = int(self.config.get("candidate_restart_limit", 3))
            if pointer.get("active") == "candidate" and self.failures >= max_failures:
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
    except KeyboardInterrupt:
        return 130
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
