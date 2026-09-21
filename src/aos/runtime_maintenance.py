"""Durable maintenance mode for Runtime V1."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, read_json

MAINTENANCE_FILENAME = "maintenance-state.json"
PAUSED_SAFE = "PAUSED_SAFE"
RUNNING = "RUNNING"


def maintenance_path(runtime_root: Path) -> Path:
    return runtime_root.expanduser().resolve() / MAINTENANCE_FILENAME


def read_maintenance(runtime_root: Path) -> Dict[str, Any]:
    value = read_json(maintenance_path(runtime_root), {})
    state = str(value.get("state") or RUNNING)
    if state not in (PAUSED_SAFE, RUNNING):
        state = PAUSED_SAFE  # corrupt/unknown state fails closed
    return {
        "contract_version": CONTRACT_VERSION,
        "state": state,
        "reason": value.get("reason"),
        "updated_at": value.get("updated_at"),
    }


def persist_maintenance(runtime_root: Path, *, paused: bool, reason: str) -> Dict[str, Any]:
    value = {
        "contract_version": CONTRACT_VERSION,
        "state": PAUSED_SAFE if paused else RUNNING,
        "reason": str(reason)[:500],
        "updated_at": utc_now(),
    }
    atomic_json(maintenance_path(runtime_root), value)
    return value


def is_paused(runtime_root: Path) -> bool:
    return read_maintenance(runtime_root)["state"] == PAUSED_SAFE
