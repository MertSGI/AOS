"""Deterministic execution authority validator for AOS-3."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from aos.validate import validate_document


class ExecutionAuthorityResult:
    """Result of execution authority validation."""

    def __init__(
        self,
        is_valid: bool,
        disposition: str,
        errors: List[str],
        execution_base_sha: Optional[str] = None,
    ):
        self.is_valid = is_valid
        self.disposition = disposition  # "ACCEPT" or "HOLD"
        self.errors = errors
        self.execution_base_sha = execution_base_sha

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "disposition": self.disposition,
            "errors": self.errors,
            "execution_base_sha": self.execution_base_sha,
        }


def validate_execution_authority(
    snapshot: Dict[str, Any],
    task: Dict[str, Any],
) -> ExecutionAuthorityResult:
    """Validate that task execution authority strictly matches canonical project snapshot authority."""
    errors: List[str] = []

    # 1. Validate canonical snapshot document against schema
    snapshot_validation = validate_document("canonical_project_snapshot", snapshot)
    if not snapshot_validation.is_valid:
        snap_errs = "; ".join(str(e) for e in snapshot_validation.errors)
        errors.append(f"Canonical project snapshot schema validation failed: {snap_errs}")
        return ExecutionAuthorityResult(
            is_valid=False,
            disposition="HOLD",
            errors=errors,
            execution_base_sha=None,
        )

    # 2. Validate canonical task document against schema
    task_validation = validate_document("task", task)
    if not task_validation.is_valid:
        task_errs = "; ".join(str(e) for e in task_validation.errors)
        errors.append(f"Canonical task schema validation failed: {task_errs}")
        return ExecutionAuthorityResult(
            is_valid=False,
            disposition="HOLD",
            errors=errors,
            execution_base_sha=None,
        )

    # 3. Snapshot ambiguity check
    if snapshot.get("has_ambiguity") or snapshot.get("ambiguity_reasons"):
        reasons = "; ".join(snapshot.get("ambiguity_reasons", []))
        errors.append(f"Canonical snapshot has ambiguity: {reasons}")

    # 4. next_action_execution_base_sha presence & format
    exec_base_sha = snapshot.get("next_action_execution_base_sha")
    if not exec_base_sha:
        errors.append("Canonical snapshot missing next_action_execution_base_sha authority")
    elif not isinstance(exec_base_sha, str) or not re.match(r"^[0-9a-f]{40}$", exec_base_sha):
        errors.append(f"Canonical execution base SHA is malformed: '{exec_base_sha}'")

    # 5. project_id exact match
    task_project_id = task.get("project_id")
    snapshot_project_id = snapshot.get("project_id")
    if task_project_id != snapshot_project_id:
        errors.append(f"Task project_id '{task_project_id}' != snapshot project_id '{snapshot_project_id}'")

    # 6. task.gate == 'AOS-3'
    task_gate = task.get("gate")
    if task_gate != "AOS-3":
        errors.append(f"Task gate '{task_gate}' is not compatible with initial execution entry (must be 'AOS-3')")

    # 7. task.risk_class == 'R1' (R1 isolated implementation only)
    task_risk = task.get("risk_class")
    if task_risk != "R1":
        errors.append(f"Task risk_class '{task_risk}' is not eligible for initial AOS-3 controlled execution (must be R1)")

    # 8. task.base_sha == snapshot.next_action_execution_base_sha
    task_base_sha = task.get("base_sha")
    if exec_base_sha and task_base_sha != exec_base_sha:
        errors.append(f"Task base_sha '{task_base_sha}' != canonical execution base SHA '{exec_base_sha}'")

    if errors:
        valid_exec_sha = exec_base_sha if isinstance(exec_base_sha, str) and re.match(r"^[0-9a-f]{40}$", exec_base_sha) else None
        return ExecutionAuthorityResult(
            is_valid=False,
            disposition="HOLD",
            errors=errors,
            execution_base_sha=valid_exec_sha,
        )

    return ExecutionAuthorityResult(
        is_valid=True,
        disposition="ACCEPT",
        errors=[],
        execution_base_sha=exec_base_sha,
    )
