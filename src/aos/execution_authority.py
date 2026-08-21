"""Deterministic execution authority validator for AOS-3."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


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

    # 1. Canonical snapshot must not have ambiguity
    if snapshot.get("has_ambiguity") or snapshot.get("ambiguity_reasons"):
        reasons = "; ".join(snapshot.get("ambiguity_reasons", []))
        errors.append(f"Canonical snapshot has ambiguity: {reasons}")

    # 2. Execution base SHA must exist in canonical snapshot
    exec_base_sha = snapshot.get("next_action_execution_base_sha")
    if not exec_base_sha:
        errors.append("Canonical snapshot missing next_action_execution_base_sha authority")
    elif not isinstance(exec_base_sha, str) or not re.match(r"^[0-9a-f]{40}$", exec_base_sha):
        errors.append(f"Canonical execution base SHA is malformed: '{exec_base_sha}'")

    # 3. Project ID match
    task_project_id = task.get("project_id")
    snapshot_project_id = snapshot.get("project_id")
    if not task_project_id:
        errors.append("Task missing project_id")
    elif task_project_id != snapshot_project_id:
        errors.append(f"Task project_id '{task_project_id}' != snapshot project_id '{snapshot_project_id}'")

    # 4. Task base_sha must equal canonical next_action_execution_base_sha
    task_base_sha = task.get("base_sha")
    if not task_base_sha:
        errors.append("Task missing base_sha")
    elif exec_base_sha and task_base_sha != exec_base_sha:
        errors.append(f"Task base_sha '{task_base_sha}' != canonical execution base SHA '{exec_base_sha}'")

    # 5. Task gate and risk class compatibility for controlled single-worker execution
    task_risk = task.get("risk_class")
    if task_risk not in ("R0", "R1"):
        errors.append(f"Task risk_class '{task_risk}' is not eligible for controlled execution (must be R0 or R1)")

    if errors:
        return ExecutionAuthorityResult(
            is_valid=False,
            disposition="HOLD",
            errors=errors,
            execution_base_sha=exec_base_sha,
        )

    return ExecutionAuthorityResult(
        is_valid=True,
        disposition="ACCEPT",
        errors=[],
        execution_base_sha=exec_base_sha,
    )
