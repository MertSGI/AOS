"""Durable, fail-closed command admission for Runtime V1.

Global maintenance remains the master switch.  This store controls which
individual command lineages may execute when that switch is open.  A missing
record is deliberately interpreted as HOLD so legacy commands cannot be
silently resumed after an upgrade or restart.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, exclusive_file_lock, read_json


class AdmissionState(str, Enum):
    ACTIVE = "ACTIVE"
    HOLD = "HOLD"
    SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class AdmissionRecord:
    command_id: str
    project_id: str
    state: str
    reason: str
    authority: str
    updated_at: str
    recovery_proof_id: Optional[str] = None
    legacy_missing_record: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CommandAdmissionStore:
    """Persistent command admission registry with atomic batch activation."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.path = self.runtime_root / "command-admission.json"
        self.lock_path = self.runtime_root / "command-admission.lock"

    def _load(self) -> Dict[str, Any]:
        value = read_json(self.path, {})
        records = value.get("records")
        if not isinstance(records, dict):
            records = {}
        return {
            "contract_version": CONTRACT_VERSION,
            "updated_at": str(value.get("updated_at") or ""),
            "records": records,
        }

    def _project_id(self, command_id: str) -> str:
        command = read_json(
            self.runtime_root / "commands" / command_id / "command.json", {}
        )
        project = command.get("project")
        if not command or not isinstance(project, dict):
            raise ValueError(f"Command not found: {command_id}")
        project_id = str(project.get("project_id") or "").strip()
        if not project_id:
            raise ValueError(f"Command has no project binding: {command_id}")
        return project_id

    def get(self, command_id: str) -> AdmissionRecord:
        payload = self._load().get("records", {}).get(command_id)
        if not isinstance(payload, dict):
            project_id = "UNKNOWN"
            try:
                project_id = self._project_id(command_id)
            except ValueError:
                pass
            return AdmissionRecord(
                command_id=command_id,
                project_id=project_id,
                state=AdmissionState.HOLD.value,
                reason="LEGACY_ADMISSION_RECORD_MISSING",
                authority="FAIL_CLOSED_DEFAULT",
                updated_at="",
                legacy_missing_record=True,
            )
        return AdmissionRecord(
            command_id=command_id,
            project_id=str(payload.get("project_id") or "UNKNOWN"),
            state=str(payload.get("state") or AdmissionState.HOLD.value),
            reason=str(payload.get("reason") or "UNSPECIFIED"),
            authority=str(payload.get("authority") or "UNKNOWN"),
            updated_at=str(payload.get("updated_at") or ""),
            recovery_proof_id=(
                str(payload["recovery_proof_id"])
                if payload.get("recovery_proof_id")
                else None
            ),
            legacy_missing_record=False,
        )

    def is_active(self, command_id: str) -> bool:
        return self.get(command_id).state == AdmissionState.ACTIVE.value

    def set_state(
        self,
        command_id: str,
        state: AdmissionState | str,
        *,
        authority: str,
        reason: str,
        recovery_proof_id: Optional[str] = None,
    ) -> AdmissionRecord:
        target = AdmissionState(str(getattr(state, "value", state)))
        project_id = self._project_id(command_id)
        with exclusive_file_lock(self.lock_path):
            document = self._load()
            current = document["records"].get(command_id)
            if (
                isinstance(current, dict)
                and current.get("state") == AdmissionState.SUPERSEDED.value
                and target != AdmissionState.SUPERSEDED
            ):
                raise ValueError(f"Superseded command cannot be reactivated: {command_id}")
            now = utc_now()
            payload = {
                "command_id": command_id,
                "project_id": project_id,
                "state": target.value,
                "reason": str(reason),
                "authority": str(authority),
                "updated_at": now,
                "recovery_proof_id": recovery_proof_id,
            }
            document["records"][command_id] = payload
            document["updated_at"] = now
            atomic_json(self.path, document)
        return self.get(command_id)

    def activate_many(
        self,
        command_ids: Iterable[str],
        *,
        authority: str,
        reason: str,
        recovery_proof_ids: Optional[Dict[str, str]] = None,
    ) -> List[AdmissionRecord]:
        """Activate a selected set in one durable transaction.

        Every binding and transition is validated before the file is changed.
        A superseded or missing command therefore makes the whole request fail.
        """
        selected = list(dict.fromkeys(str(item) for item in command_ids))
        if not selected:
            raise ValueError("At least one command_id is required")
        projects = {command_id: self._project_id(command_id) for command_id in selected}
        proof_ids = recovery_proof_ids or {}
        with exclusive_file_lock(self.lock_path):
            document = self._load()
            for command_id in selected:
                current = document["records"].get(command_id)
                if isinstance(current, dict) and current.get("state") == AdmissionState.SUPERSEDED.value:
                    raise ValueError(f"Superseded command cannot be reactivated: {command_id}")
            now = utc_now()
            for command_id in selected:
                document["records"][command_id] = {
                    "command_id": command_id,
                    "project_id": projects[command_id],
                    "state": AdmissionState.ACTIVE.value,
                    "reason": str(reason),
                    "authority": str(authority),
                    "updated_at": now,
                    "recovery_proof_id": proof_ids.get(command_id),
                }
            document["updated_at"] = now
            atomic_json(self.path, document)
        return [self.get(command_id) for command_id in selected]

    def activate_system_defect_remediated(
        self,
        command_id: str,
        *,
        defect_class: str,
        repaired_runtime_sha: str,
        prior_terminal_state: str,
        prior_failure_class: str,
        canonical_authority_valid: bool,
        production: str,
    ) -> AdmissionRecord:
        """Bounded reactivation of a command stopped by a known system defect."""
        allowed_defects = {"RECOVERY_CHURN_GUARD", "DELIVERY_CLOSURE_MISSING"}
        if defect_class not in allowed_defects:
            raise ValueError(f"defect_class must be one of {allowed_defects}, got: {defect_class}")
        if prior_terminal_state != "HUMAN_REQUIRED":
            raise ValueError(f"prior_terminal_state must be HUMAN_REQUIRED, got: {prior_terminal_state}")
        if prior_failure_class != defect_class:
            raise ValueError(f"prior_failure_class ({prior_failure_class}) must match defect_class ({defect_class})")
        if not canonical_authority_valid:
            raise ValueError("canonical_authority_valid must be True")
        if production != "NO_GO":
            raise ValueError(f"production must be NO_GO, got: {production}")
        if not repaired_runtime_sha or len(repaired_runtime_sha) < 7:
            raise ValueError("repaired_runtime_sha is required")

        # Update command state file to RECOVERING so worker resumes from checkpoint
        cmd_state_path = self.runtime_root / "commands" / command_id / "state.json"
        if cmd_state_path.is_file():
            state_data = read_json(cmd_state_path, {})
            state_data["state"] = "RECOVERING"
            state_data["disposition"] = "SYSTEM_DEFECT_REMEDIATED"
            state_data["repaired_runtime_sha"] = repaired_runtime_sha
            state_data["defect_class"] = defect_class
            state_data["updated_at"] = utc_now()
            atomic_json(cmd_state_path, state_data)

        # Update admission store
        return self.set_state(
            command_id,
            AdmissionState.ACTIVE,
            authority="SYSTEM_DEFECT_REMEDIATED",
            reason=f"System defect remediated: {defect_class} via runtime SHA {repaired_runtime_sha}",
            recovery_proof_id=None,
        )

    def snapshot(self) -> Dict[str, Any]:
        document = self._load()
        return {
            **document,
            "missing_record_policy": AdmissionState.HOLD.value,
            "global_maintenance_is_master": True,
        }
