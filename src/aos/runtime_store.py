"""Durable structured state/event store for AOS Runtime V1."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from aos.runtime_contract import CONTRACT_VERSION, RuntimeEvent, utc_now


import time
import uuid


def atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 7:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.5))


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if default is None:
        default = {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except (OSError, ValueError, json.JSONDecodeError):
        return dict(default)


@contextmanager
def exclusive_file_lock(path: Path, *, blocking: bool = True) -> Iterator[Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    handle = path.open("r+b")
    try:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as exc:
                raise BlockingIOError(f"File lock busy: {path}") from exc
        else:
            import fcntl
            flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError as exc:
                raise BlockingIOError(f"File lock busy: {path}") from exc
        yield handle
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            handle.close()


class RuntimeStore:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.commands_root = self.runtime_root / "commands"
        self.commands_root.mkdir(parents=True, exist_ok=True)

    def command_dir(self, command_id: str) -> Path:
        return self.commands_root / command_id

    def resource_os_dir(self, command_id: Optional[str] = None) -> Path:
        root = (
            self.command_dir(command_id) / "project-runtime" / "resource-os"
            if command_id else self.runtime_root / "resource-os"
        )
        root.mkdir(parents=True, exist_ok=True)
        return root

    def create_command(self, command: Dict[str, Any]) -> Path:
        command_id = str(command["command_id"])
        root = self.command_dir(command_id)
        with exclusive_file_lock(self.runtime_root / "commands.lock"):
            if root.exists():
                raise ValueError(f"Command already exists: {command_id}")
            root.mkdir(parents=True, exist_ok=False)
            atomic_json(root / "command.json", command)
            atomic_json(root / "state.json", {
                "contract_version": CONTRACT_VERSION,
                "command_id": command_id,
                "state": "QUEUED",
                "worker_pid": None,
                "attempts": 0,
                "completed_batch_count": 0,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "production": "NO_GO",
                "ag_invocation_count": 0,
            })
            atomic_json(root / "event-seq.json", {"next_seq": 1})
        self.append_event(command_id, "command.accepted", {
            "project_id": command["project"]["project_id"],
            "goal": command["goal"],
            "run_plan_required": False,
        })
        return root

    def read_command(self, command_id: str) -> Dict[str, Any]:
        return read_json(self.command_dir(command_id) / "command.json")

    def read_state(self, command_id: str) -> Dict[str, Any]:
        return read_json(self.command_dir(command_id) / "state.json")

    def write_state(self, command_id: str, **updates: Any) -> Dict[str, Any]:
        root = self.command_dir(command_id)
        with exclusive_file_lock(root / "state.lock"):
            state = read_json(root / "state.json")
            state.update(updates)
            state["contract_version"] = CONTRACT_VERSION
            state["command_id"] = command_id
            state["updated_at"] = utc_now()
            state["production"] = "NO_GO"
            state["ag_invocation_count"] = 0
            atomic_json(root / "state.json", state)
            return state

    def append_event(self, command_id: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        root = self.command_dir(command_id)
        with exclusive_file_lock(root / "events.lock"):
            seq_state = read_json(root / "event-seq.json", {"next_seq": 1})
            seq = int(seq_state.get("next_seq", 1))
            event = RuntimeEvent(
                command_id=command_id,
                event_type=event_type,
                seq=seq,
                payload=dict(payload),
            ).to_dict()
            events_path = root / "events.jsonl"
            with events_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            atomic_json(root / "event-seq.json", {"next_seq": seq + 1})
            return event

    def read_events(self, command_id: str, *, after_seq: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        path = self.command_dir(command_id) / "events.jsonl"
        if not path.is_file():
            return []
        events: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if int(event.get("seq", 0)) <= after_seq:
                continue
            events.append(event)
            if len(events) >= limit:
                break
        return events

    def write_result(self, command_id: str, result: Dict[str, Any]) -> None:
        atomic_json(self.command_dir(command_id) / "result.json", result)

    def read_result(self, command_id: str) -> Dict[str, Any]:
        return read_json(self.command_dir(command_id) / "result.json")

    def command_snapshot(self, command_id: str) -> Dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "command": self.read_command(command_id),
            "state": self.read_state(command_id),
            "result": self.read_result(command_id) or None,
        }

    def list_command_ids(self) -> List[str]:
        if not self.commands_root.is_dir():
            return []
        return sorted(p.name for p in self.commands_root.iterdir() if p.is_dir())
