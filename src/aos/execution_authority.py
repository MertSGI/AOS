"""Deterministic execution authority validator for controlled execution."""

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

    # 6. task.gate == snapshot.current_milestone
    task_gate = task.get("gate")
    snapshot_milestone = snapshot.get("current_milestone")
    if not task_gate or task_gate != snapshot_milestone:
        errors.append(f"Task gate '{task_gate}' does not match canonical snapshot milestone '{snapshot_milestone}'")

    # 7. task.risk_class check under DEC-022 HumanGatePolicy
    from aos.human_gate_policy import evaluate_human_gate_policy
    # Derive deterministic execution-boundary facts strictly from task contract
    worker_reqs = task.get("worker_requirements", {})
    isolated_worktree = bool(isinstance(worker_reqs, dict) and worker_reqs.get("isolated_worktree") is True)
    environment = worker_reqs.get("environment") if isinstance(worker_reqs, dict) else None
    is_non_prod = (environment == "non_production")
    is_isolated_non_prod = isolated_worktree and is_non_prod

    exec_context = {
        "is_isolated_non_prod": is_isolated_non_prod,
        "is_accepted_envelope": isolated_worktree,
    }
    gate_eval = evaluate_human_gate_policy(task, project_descriptor=snapshot, context=exec_context)
    if gate_eval.decision not in ("AUTO_EXECUTE", "AUTO_REMEDIATE"):
        errors.append(
            f"Execution authority human gate policy returned '{gate_eval.decision}' ({', '.join(gate_eval.reason_codes)})"
        )

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
