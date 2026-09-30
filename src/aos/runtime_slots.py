"""Atomic candidate/stable slot metadata for AOS Runtime V1."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.process_utils import process_alive
from aos.runtime_store import atomic_json, read_json
from aos.knowledge.hooks import execution_context_preflight
from aos.knowledge.accepted_work import assert_accepted_work_receipted
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import record_live_promotion_receipt, record_rollback_receipt
from aos.knowledge.runtime_transitions import (
    abort_transition,
    clear_transition_marker,
    prepare_transition,
    transition_identity,
    transition_marker,
    transition_status,
    unresolved_transition_intents,
)


class RuntimeTransitionIncompleteError(RuntimeError):
    """Normal slot use is forbidden while a prepared transition is unresolved."""


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
    def __init__(
        self,
        root: Path,
        *,
        knowledge_ledger: Optional[KnowledgeLedger] = None,
        prepared_transition_id: Optional[str] = None,
        prepared_transition_owner_pid: Optional[int] = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.slots = self.root / "slots"
        self.slots.mkdir(parents=True, exist_ok=True)
        self.pointer = self.root / "active-slot.json"
        self.knowledge_ledger = knowledge_ledger
        self.prepared_transition_id = prepared_transition_id
        self.prepared_transition_owner_pid = prepared_transition_owner_pid

    def _prepared_activation_allowed(self, value: Dict[str, Any]) -> bool:
        return bool(
            value.get("transition_state") == "INCOMPLETE_HOLD"
            and value.get("transition_operation") == "ACTIVATE"
            and value.get("transition_id") == self.prepared_transition_id
            and self.prepared_transition_owner_pid
            and process_alive(self.prepared_transition_owner_pid)
        )

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
        prepared_allowed = self._prepared_activation_allowed(value)
        if value.get("transition_state") == "INCOMPLETE_HOLD" and not prepared_allowed:
            raise RuntimeTransitionIncompleteError(
                f"RUNTIME_TRANSITION_INCOMPLETE:{value.get('transition_id') or 'UNKNOWN'}"
            )
        if self.knowledge_ledger is not None:
            unresolved = unresolved_transition_intents(
                self.knowledge_ledger,
                boundary="aos.runtime_slots",
                resource=str(self.pointer),
            )
            unresolved = tuple(
                event for event in unresolved
                if event.get("claims", {}).get("transition_id") != self.prepared_transition_id
                or not prepared_allowed
            )
            if unresolved:
                transition_id = unresolved[-1]["claims"]["transition_id"]
                raise RuntimeTransitionIncompleteError(
                    f"RUNTIME_TRANSITION_INCOMPLETE:{transition_id}"
                )
        return value

    def reconcile_incomplete_transition(self) -> Dict[str, Any]:
        """Deterministically finish or abort an interrupted local slot transition."""
        if self.knowledge_ledger is None:
            raise ValueError("KCP_REQUIRED_BUT_UNAVAILABLE")
        current = read_json(self.pointer, {})
        marker_id = str(current.get("transition_id") or "")
        if marker_id:
            status = transition_status(self.knowledge_ledger, marker_id)
            if status["completion"] is not None or status["abort"] is not None:
                final = clear_transition_marker(current)
                atomic_json(self.pointer, final)
                current = final
        unresolved = unresolved_transition_intents(
            self.knowledge_ledger,
            boundary="aos.runtime_slots",
            resource=str(self.pointer),
        )
        for intent in unresolved:
            claims = intent["claims"]
            transition_id = str(claims["transition_id"])
            previous = dict(claims["previous_state"])
            target = dict(claims["target_state"])
            current = read_json(self.pointer, {})
            comparable = clear_transition_marker(current)
            if comparable not in (previous, target) and current not in (previous, target):
                held = {**current, **transition_marker(transition_id, str(claims["operation"]))}
                atomic_json(self.pointer, held)
                raise RuntimeTransitionIncompleteError(
                    f"RUNTIME_TRANSITION_RECONCILIATION_REQUIRED:{transition_id}"
                )
            atomic_json(self.pointer, previous)
            try:
                abort_transition(
                    self.knowledge_ledger,
                    transition_id=transition_id,
                    boundary="aos.runtime_slots",
                    result_sha=str(intent["result_sha"]),
                    reason="restart reconciliation restored previous slot pointer",
                    recovered_state=previous,
                )
            except Exception:
                atomic_json(
                    self.pointer,
                    {**previous, **transition_marker(transition_id, str(claims["operation"]))},
                )
                raise
            current = previous
        return self.read_pointer()

    def active_slot(self) -> SlotRecord:
        pointer = self.read_pointer()
        key = "candidate_slot_id" if pointer["active"] == "candidate" else "stable_slot_id"
        return self.read_slot(str(pointer[key]))

    def rollback(self, *, reason: str) -> Dict[str, Any]:
        pointer = self.read_pointer()
        stable = self.read_slot(str(pointer["stable_slot_id"]))
        if self.knowledge_ledger is None:
            raise ValueError("KCP_REQUIRED_BUT_UNAVAILABLE")
        if not stable.source_sha or len(stable.source_sha) != 40:
            raise ValueError("slot rollback knowledge preflight requires stable source SHA")
        execution_context_preflight(
            self.knowledge_ledger,
            project_id="AOS",
            task_class="RUNTIME_ROLLBACK",
            module_ids=["RuntimeSupervisor", "RuntimeDeploy"],
            paths=[str(self.pointer)],
            base_sha=stable.source_sha,
        )
        previous = dict(pointer)
        target = dict(pointer)
        target["active"] = "stable"
        target["promotion_state"] = "ROLLED_BACK"
        target["rollback_reason"] = str(reason)[:1000]
        target["updated_at"] = utc_now()
        transition_id = transition_identity(
            "SLOT_ROLLBACK",
            str(self.pointer),
            pointer.get("stable_slot_id"),
            pointer.get("candidate_slot_id"),
            stable.source_sha,
            str(reason)[:1000],
        )
        existing = transition_status(self.knowledge_ledger, transition_id)
        if existing["completion"] is not None:
            if pointer.get("active") == "stable" and pointer.get("rollback_reason") == str(reason)[:1000]:
                return pointer
            raise RuntimeTransitionIncompleteError(
                f"RUNTIME_TRANSITION_COMPLETION_STATE_MISMATCH:{transition_id}"
            )
        prepare_transition(
            self.knowledge_ledger,
            transition_id=transition_id,
            operation="ROLLBACK",
            boundary="aos.runtime_slots",
            resource=str(self.pointer),
            result_sha=stable.source_sha,
            previous_state=previous,
            target_state=target,
            module_ids=["RuntimeSupervisor", "RuntimeDeploy"],
            identity={"restored_slot_id": stable.slot_id},
        )
        completion = False
        try:
            atomic_json(self.pointer, {**target, **transition_marker(transition_id, "ROLLBACK")})
            record_rollback_receipt(
                self.knowledge_ledger,
                project_id="AOS",
                idempotency_key=f"slot-rollback:{transition_id}",
                agent_class="AOS_NATIVE",
                tool_name="aos.runtime_slots",
                result_sha=stable.source_sha,
                module_ids=["RuntimeSupervisor"],
                claims={
                    "transition_id": transition_id,
                    "reason": reason,
                    "restored_slot_id": stable.slot_id,
                },
            )
            completion = True
            atomic_json(self.pointer, target)
            return target
        except Exception:
            if completion or transition_status(self.knowledge_ledger, transition_id)["completion"]:
                raise
            atomic_json(self.pointer, previous)
            try:
                abort_transition(
                    self.knowledge_ledger,
                    transition_id=transition_id,
                    boundary="aos.runtime_slots",
                    result_sha=stable.source_sha,
                    reason="slot rollback failed before completion",
                    recovered_state=previous,
                )
            except Exception:
                atomic_json(
                    self.pointer,
                    {**previous, **transition_marker(transition_id, "ROLLBACK")},
                )
            raise

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
        candidate = self.read_slot(str(pointer["candidate_slot_id"]))
        if self.knowledge_ledger is None:
            raise ValueError("KCP_REQUIRED_BUT_UNAVAILABLE")
        if not candidate.source_sha:
            raise ValueError("candidate promotion requires exact source SHA")
        execution_context_preflight(
            self.knowledge_ledger,
            project_id="AOS",
            task_class="RUNTIME_PROMOTION",
            module_ids=["RuntimeSupervisor", "RuntimeDeploy"],
            paths=[str(self.pointer)],
            base_sha=candidate.source_sha,
        )
        assert_accepted_work_receipted(
            self.knowledge_ledger,
            project_id="AOS",
            result_sha=candidate.source_sha,
        )
        transition_id = transition_identity(
            "SLOT_PROMOTION", str(self.pointer), proof_id, candidate.source_sha, candidate.slot_id
        )
        existing = transition_status(self.knowledge_ledger, transition_id)
        if existing["completion"] is not None:
            if (
                pointer.get("active") == "stable"
                and pointer.get("stable_slot_id") == candidate.slot_id
                and pointer.get("promotion_proof_id") == proof_id
            ):
                return pointer
            raise RuntimeTransitionIncompleteError(
                f"RUNTIME_TRANSITION_COMPLETION_STATE_MISMATCH:{transition_id}"
            )
        previous = dict(pointer)
        target = dict(pointer)
        target["stable_slot_id"] = target["candidate_slot_id"]
        target["active"] = "stable"
        target["promotion_state"] = "STABLE"
        target["promotion_proof_id"] = proof_id
        target["updated_at"] = utc_now()
        prepare_transition(
            self.knowledge_ledger,
            transition_id=transition_id,
            operation="PROMOTE",
            boundary="aos.runtime_slots",
            resource=str(self.pointer),
            result_sha=candidate.source_sha,
            previous_state=previous,
            target_state=target,
            module_ids=["RuntimeSupervisor", "RuntimeDeploy"],
            identity={"proof_id": proof_id, "candidate_slot_id": candidate.slot_id},
        )
        completion = False
        try:
            atomic_json(self.pointer, {**target, **transition_marker(transition_id, "PROMOTE")})
            record_live_promotion_receipt(
                self.knowledge_ledger,
                project_id="AOS",
                idempotency_key=f"slot-promotion:{transition_id}",
                agent_class="AOS_NATIVE",
                tool_name="aos.runtime_slots",
                result_sha=candidate.source_sha,
                module_ids=["RuntimeSupervisor", "RuntimeDeploy"],
                evidence_refs=[proof_id],
                claims={
                    "transition_id": transition_id,
                    "promotion_state": "STABLE",
                    "slot_id": candidate.slot_id,
                },
            )
            completion = True
            atomic_json(self.pointer, target)
            return target
        except Exception:
            if completion or transition_status(self.knowledge_ledger, transition_id)["completion"]:
                raise
            atomic_json(self.pointer, previous)
            try:
                abort_transition(
                    self.knowledge_ledger,
                    transition_id=transition_id,
                    boundary="aos.runtime_slots",
                    result_sha=candidate.source_sha,
                    reason="slot promotion failed before completion",
                    recovered_state=previous,
                )
            except Exception:
                atomic_json(
                    self.pointer,
                    {**previous, **transition_marker(transition_id, "PROMOTE")},
                )
            raise
