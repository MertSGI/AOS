"""Atomic candidate/stable slot metadata for AOS Runtime V1."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, read_json


@dataclass(frozen=True)
class SlotRecord:
    slot_id: str
    kind: str
    command: tuple[str, ...]
    source_sha: Optional[str]
    health_url: Optional[str]
    config_path: Optional[str]
    created_at: str

    @classmethod
    def from_mapping(cls, value: Dict[str, Any]) -> "SlotRecord":
        command = value.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("Slot command must be a non-empty string array")
        slot_id = str(value.get("slot_id", "")).strip()
        if not slot_id:
            raise ValueError("slot_id is required")
        kind = str(value.get("kind", "")).strip()
        if kind not in ("runtime_v1", "legacy_host"):
            raise ValueError(f"Unsupported slot kind: {kind}")
        health_url = value.get("health_url")
        if health_url is not None and not isinstance(health_url, str):
            raise ValueError("health_url must be text or null")
        return cls(
            slot_id=slot_id,
            kind=kind,
            command=tuple(command),
            source_sha=str(value.get("source_sha")) if value.get("source_sha") else None,
            health_url=health_url,
            config_path=str(value.get("config_path")) if value.get("config_path") else None,
            created_at=str(value.get("created_at") or utc_now()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "slot_id": self.slot_id,
            "kind": self.kind,
            "command": list(self.command),
            "source_sha": self.source_sha,
            "health_url": self.health_url,
            "config_path": self.config_path,
            "created_at": self.created_at,
        }


class SlotManager:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.slots = self.root / "slots"
        self.slots.mkdir(parents=True, exist_ok=True)
        self.pointer = self.root / "active-slot.json"

    def write_slot(self, record: SlotRecord) -> Path:
        path = self.slots / f"{record.slot_id}.json"
        atomic_json(path, record.to_dict())
        return path

    def read_slot(self, slot_id: str) -> SlotRecord:
        value = read_json(self.slots / f"{slot_id}.json")
        if not value:
            raise ValueError(f"Slot not found: {slot_id}")
        return SlotRecord.from_mapping(value)

    def initialize(self, *, stable_slot_id: str, candidate_slot_id: str, active: str = "candidate") -> Dict[str, Any]:
        if active not in ("stable", "candidate"):
            raise ValueError("active must be stable or candidate")
        pointer = {
            "contract_version": CONTRACT_VERSION,
            "stable_slot_id": stable_slot_id,
            "candidate_slot_id": candidate_slot_id,
            "active": active,
            "promotion_state": "TRIAL" if active == "candidate" else "STABLE",
            "updated_at": utc_now(),
        }
        atomic_json(self.pointer, pointer)
        return pointer

    def read_pointer(self) -> Dict[str, Any]:
        value = read_json(self.pointer)
        if value.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("Invalid or missing active slot pointer")
        if value.get("active") not in ("stable", "candidate"):
            raise ValueError("Invalid active slot")
        return value

    def active_slot(self) -> SlotRecord:
        pointer = self.read_pointer()
        key = "candidate_slot_id" if pointer["active"] == "candidate" else "stable_slot_id"
        return self.read_slot(str(pointer[key]))

    def rollback(self, *, reason: str) -> Dict[str, Any]:
        pointer = self.read_pointer()
        pointer["active"] = "stable"
        pointer["promotion_state"] = "ROLLED_BACK"
        pointer["rollback_reason"] = str(reason)[:1000]
        pointer["updated_at"] = utc_now()
        atomic_json(self.pointer, pointer)
        return pointer

    def mark_candidate_healthy(self) -> Dict[str, Any]:
        pointer = self.read_pointer()
        if pointer["active"] != "candidate":
            return pointer
        pointer["candidate_health"] = "HEALTHY"
        pointer["updated_at"] = utc_now()
        atomic_json(self.pointer, pointer)
        return pointer

    def promote_candidate(self, *, proof_id: str) -> Dict[str, Any]:
        """Atomically promote candidate only after an external accepted proof boundary.

        Runtime V1 itself never fabricates acceptance; callers must provide a proof id.
        """
        if not proof_id or len(proof_id) > 200:
            raise ValueError("A bounded proof_id is required for promotion")
        pointer = self.read_pointer()
        pointer["stable_slot_id"] = pointer["candidate_slot_id"]
        pointer["active"] = "stable"
        pointer["promotion_state"] = "STABLE"
        pointer["promotion_proof_id"] = proof_id
        pointer["updated_at"] = utc_now()
        atomic_json(self.pointer, pointer)
        return pointer
