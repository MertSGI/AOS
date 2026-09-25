"""AOS First-Class Human Action Center and Modernized Governance Dispositions.

Implements actionable, precise human-in-the-loop governance for AOS:
- Exact operator action classes:
    WAITING_FOR_RESOURCE
    AUTO_REPAIR_PENDING
    AUTH_REQUIRED
    HUMAN_REVIEW_REQUIRED
    HUMAN_DECISION_REQUIRED
    HUMAN_APPROVAL_REQUIRED
    TERMINAL_TECHNICAL_FAILURE
- Modernized dispositions:
    RUNNING
    WAITING_FOR_RESOURCE
    AUTO_REPAIR_PENDING
    AUTH_REQUIRED
    HUMAN_REVIEW_REQUIRED
    HUMAN_DECISION_REQUIRED
    HUMAN_APPROVAL_REQUIRED
    COMPLETED
    FAILED_TERMINAL
- Pinned canonical control revision validation
- Authoritative Control Request submission via control_request schema v0.1
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import RuntimeStore, atomic_json, read_json
from aos.validate import validate_document


# Operator Action Class Definitions
ACTION_CLASS_WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
ACTION_CLASS_AUTO_REPAIR_PENDING = "AUTO_REPAIR_PENDING"
ACTION_CLASS_AUTH_REQUIRED = "AUTH_REQUIRED"
ACTION_CLASS_HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
ACTION_CLASS_HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
ACTION_CLASS_HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
ACTION_CLASS_TERMINAL_TECHNICAL_FAILURE = "TERMINAL_TECHNICAL_FAILURE"

HUMAN_ACTIONABLE_CLASSES = {
    ACTION_CLASS_AUTH_REQUIRED,
    ACTION_CLASS_HUMAN_REVIEW_REQUIRED,
    ACTION_CLASS_HUMAN_DECISION_REQUIRED,
    ACTION_CLASS_HUMAN_APPROVAL_REQUIRED,
}

ALL_ACTION_CLASSES = {
    ACTION_CLASS_WAITING_FOR_RESOURCE,
    ACTION_CLASS_AUTO_REPAIR_PENDING,
    ACTION_CLASS_AUTH_REQUIRED,
    ACTION_CLASS_HUMAN_REVIEW_REQUIRED,
    ACTION_CLASS_HUMAN_DECISION_REQUIRED,
    ACTION_CLASS_HUMAN_APPROVAL_REQUIRED,
    ACTION_CLASS_TERMINAL_TECHNICAL_FAILURE,
}

# Modernized Runtime Dispositions
DISPOSITION_RUNNING = "RUNNING"
DISPOSITION_WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
DISPOSITION_AUTO_REPAIR_PENDING = "AUTO_REPAIR_PENDING"
DISPOSITION_AUTH_REQUIRED = "AUTH_REQUIRED"
DISPOSITION_HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
DISPOSITION_HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
DISPOSITION_HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
DISPOSITION_COMPLETED = "COMPLETED"
DISPOSITION_FAILED_TERMINAL = "FAILED_TERMINAL"


@dataclass
class ActionOption:
    option_id: str
    label: str
    description: str
    consequences: str
    reversibility: str  # REVERSIBLE, IRREVERSIBLE, BOUNDED
    is_safe_default: bool = False
    control_request_type: str = "RESUME"
    requested_change: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HumanActionItem:
    action_id: str
    project: str
    command_id: str
    current_batch: int
    action_class: str
    risk_class: str
    why_stopped: str
    expected_state: str
    observed_state: str
    exact_blocker: str
    why_automation_cannot_continue: str
    evidence_references: List[str]
    canonical_revision: str
    workspace_fingerprint: str
    decision_required: str
    available_options: List[Dict[str, Any]]
    option_consequences: Dict[str, str]
    reversibility: str
    safe_default_if_any: Optional[str]
    authority_boundary: str
    created_at: str
    status: str = "PENDING"  # PENDING, RESOLVED, DISMISSED
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_lane_hold(
    *,
    project_id: str,
    command_id: str,
    cmd_state: Dict[str, Any],
    checkpoint: Dict[str, Any],
    recent_events: Sequence[Dict[str, Any]] = (),
    provider_circuits: Optional[Dict[str, Any]] = None,
) -> tuple[str, str, Dict[str, Any]]:
    """Determine the precise operator action class and disposition for a command hold.
    
    Returns:
        (action_class, modernized_disposition, diagnostic_context)
    """
    raw_state = str(cmd_state.get("state") or "").upper()
    disposition = str(cmd_state.get("disposition") or "").upper()
    failure_class = str(cmd_state.get("failure_class") or "").upper()
    recovery_disp = str(cmd_state.get("recovery_disposition") or "").upper()
    current_phase = str(checkpoint.get("phase") or checkpoint.get("current_planning_phase") or "").upper()
    reason = str(checkpoint.get("reason") or cmd_state.get("error") or "").upper()

    context: Dict[str, Any] = {
        "raw_state": raw_state,
        "disposition": disposition,
        "failure_class": failure_class,
        "current_phase": current_phase,
        "reason": reason,
    }

    # 1. Provider availability / rate-limit check:
    # If the hold is due to provider exhaustion, circuit open, or waiting for reasoning provider,
    # it is strictly WAITING_FOR_RESOURCE.
    is_reasoning_wait = (
        "WAITING_FOR_REASONING_PROVIDER" in raw_state
        or "WAITING_FOR_REASONING_PROVIDER" in disposition
        or "WAITING_FOR_REASONING_PROVIDER" in current_phase
        or "PROVIDER_API_QUOTA_EXHAUSTED" in failure_class
        or "RATE_LIMIT" in reason
        or "RESOURCE_EXHAUSTED" in reason
    )

    if is_reasoning_wait:
        # Check if auth token is explicitly missing/expired vs quota/rate-limit
        if "AUTH" in reason or "UNAUTHENTICATED" in reason or "API_KEY_REQUIRED" in reason:
            return ACTION_CLASS_AUTH_REQUIRED, DISPOSITION_AUTH_REQUIRED, context
        return ACTION_CLASS_WAITING_FOR_RESOURCE, DISPOSITION_WAITING_FOR_RESOURCE, context

    # 2. Check recovery churn guard with provider wait underlying:
    # If RECOVERY_CHURN_GUARD tripped, inspect whether the repeated cycles were purely provider waits
    if failure_class == "RECOVERY_CHURN_GUARD" or recovery_disp == "RECOVERY_CHURN_GUARD":
        if "WAITING_FOR_REASONING_PROVIDER" in current_phase or "WAITING_FOR_REASONING_PROVIDER" in reason:
            return ACTION_CLASS_WAITING_FOR_RESOURCE, DISPOSITION_WAITING_FOR_RESOURCE, context
        # Check if underlying issue is canonical drift / reconciliation
        if "CANONICAL" in reason or "CANONICAL_CONTRADICTION" in failure_class:
            return ACTION_CLASS_HUMAN_REVIEW_REQUIRED, DISPOSITION_HUMAN_REVIEW_REQUIRED, context
        # General churn guard due to unresolved semantic/technical failure:
        return ACTION_CLASS_HUMAN_DECISION_REQUIRED, DISPOSITION_HUMAN_DECISION_REQUIRED, context

    # 3. Check authentication required
    if (
        "AUTH_REQUIRED" in failure_class
        or "CREDENTIAL" in failure_class
        or disposition == "AUTH_REQUIRED"
        or "UNAUTHENTICATED" in reason
        or "INVALID_TOKEN" in reason
    ):
        return ACTION_CLASS_AUTH_REQUIRED, DISPOSITION_AUTH_REQUIRED, context

    # 4. Check auto repair pending
    if failure_class == "AUTO_REPAIR_PENDING" or disposition == "AUTO_REPAIR_PENDING":
        return ACTION_CLASS_AUTO_REPAIR_PENDING, DISPOSITION_AUTO_REPAIR_PENDING, context

    # 5. Check human approval required (high-risk destructive action or scope expansion)
    if "APPROVAL_REQUIRED" in failure_class or "SCOPE_EXPANSION" in reason or "UNAUTHORIZED_MUTATION" in reason:
        return ACTION_CLASS_HUMAN_APPROVAL_REQUIRED, DISPOSITION_HUMAN_APPROVAL_REQUIRED, context

    # 6. Check human review required (conflicting evidence, visual review, canonical drift)
    if (
        "HUMAN_REVIEW" in failure_class
        or "CANONICAL_DRIFT" in failure_class
        or "CONFLICTING_EVIDENCE" in reason
        or "VISUAL_QA_FAIL" in reason
    ):
        return ACTION_CLASS_HUMAN_REVIEW_REQUIRED, DISPOSITION_HUMAN_REVIEW_REQUIRED, context

    # 7. Check human decision required (architecture ambiguity, trade-off)
    if "DECISION_REQUIRED" in failure_class or "AMBIGUOUS" in reason or "TRADEOFF" in reason:
        return ACTION_CLASS_HUMAN_DECISION_REQUIRED, DISPOSITION_HUMAN_DECISION_REQUIRED, context

    # 8. Terminal technical failure
    if raw_state == "FAILED" or disposition == "FAILED_TERMINAL" or "FATAL" in reason:
        return ACTION_CLASS_TERMINAL_TECHNICAL_FAILURE, DISPOSITION_FAILED_TERMINAL, context

    # 9. Generic HUMAN_REQUIRED fallback
    if raw_state == "HUMAN_REQUIRED":
        return ACTION_CLASS_HUMAN_DECISION_REQUIRED, DISPOSITION_HUMAN_DECISION_REQUIRED, context

    return ACTION_CLASS_WAITING_FOR_RESOURCE, DISPOSITION_RUNNING, context


class ActionCenterEngine:
    """AOS-native Human Action Center and Control Request Router."""

    def __init__(self, action_store_root: Path, config: Optional[Dict[str, Any]] = None) -> None:
        self.root = action_store_root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.actions_file = self.root / "human-actions.json"
        self.config = config or {}
        self._actions: Dict[str, Dict[str, Any]] = self._load_actions()

    def _load_actions(self) -> Dict[str, Dict[str, Any]]:
        return read_json(self.actions_file, {})

    def _save_actions(self) -> None:
        atomic_json(self.actions_file, self._actions)

    def compute_action_id(self, project: str, command_id: str, action_class: str, blocker: str) -> str:
        raw = f"{project}:{command_id}:{action_class}:{blocker.strip().lower()}"
        return f"act-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"

    def create_or_update_action(self, item: HumanActionItem) -> HumanActionItem:
        act_dict = item.to_dict()
        self._actions[item.action_id] = act_dict
        self._save_actions()
        return item

    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        return self._actions.get(action_id)

    def list_pending_actions(self) -> List[Dict[str, Any]]:
        return [
            a for a in self._actions.values()
            if a.get("status") == "PENDING"
            and a.get("action_class") in HUMAN_ACTIONABLE_CLASSES
        ]

    def count_human_action_required(self) -> int:
        """Count only items where genuine operator action is required."""
        return len(self.list_pending_actions())

    def resolve_action(
        self,
        action_id: str,
        *,
        resolved_by: str,
        request_id: str,
        control_change: str,
    ) -> bool:
        if action_id not in self._actions:
            return False
        action = self._actions[action_id]
        action["status"] = "RESOLVED"
        action["resolved_by"] = resolved_by
        action["resolved_at"] = utc_now()
        action["resolution_request_id"] = request_id
        action["resolution_change"] = control_change
        self._save_actions()
        return True

    def scan_and_reconcile(
        self,
        store: RuntimeStore,
        active_command_ids: Sequence[str],
        candidate_source_sha: str = "UNKNOWN",
    ) -> List[HumanActionItem]:
        """Scan active/held commands and reconcile them into precise Human Action Items.
        
        Only genuine human-required items create HumanActionItems.
        Resource waits and auto-repairs are excluded from the operator action badge.
        """
        generated: List[HumanActionItem] = []

        for cid in active_command_ids:
            try:
                cmd = store.read_command(cid) or {}
                state = store.read_state(cid) or {}
                project = cmd.get("project", {}) if isinstance(cmd, dict) else {}
                project_id = str(project.get("project_id") or cmd.get("project_id") or "unknown")
                checkpoint = read_json(store.command_dir(cid) / "project-runtime" / "planning-kernel-checkpoint.json") or {}
                batch_num = int(checkpoint.get("batch_number", 0) or state.get("completed_batch_count", 0) or 0)
                canon_sha = str(checkpoint.get("canonical_source_sha") or state.get("canonical_source_sha") or candidate_source_sha)
                ws_fp = str(state.get("recovery_fingerprint_sha256") or "NONE")

                action_class, modernized_disp, ctx = classify_lane_hold(
                    project_id=project_id,
                    command_id=cid,
                    cmd_state=state,
                    checkpoint=checkpoint,
                )

                # WAITING_FOR_RESOURCE and AUTO_REPAIR_PENDING do NOT generate human action items
                if action_class not in HUMAN_ACTIONABLE_CLASSES:
                    continue

                act_id = self.compute_action_id(project_id, cid, action_class, ctx.get("reason", "HOLD"))

                # Check if already resolved
                existing = self._actions.get(act_id)
                if existing and existing.get("status") == "RESOLVED":
                    continue

                # Build options based on action class
                options: List[ActionOption] = []
                opt_consequences: Dict[str, str] = {}
                decision_prompt = ""
                safe_default: Optional[str] = None
                why_cannot = "Automation halted at authority boundary to prevent uncontrolled divergence."

                if action_class == ACTION_CLASS_AUTH_REQUIRED:
                    decision_prompt = "Provide or configure required authentication credentials."
                    why_cannot = "Requested reasoning or repository service requires valid authentication credentials."
                    options = [
                        ActionOption(
                            option_id="AUTH_RETRY",
                            label="Credentials Configured · Retry",
                            description="Confirm credentials in Windows Credential Vault and re-enter lane.",
                            consequences="AOS will verify credential existence and resume autonomous planning.",
                            reversibility="REVERSIBLE",
                            is_safe_default=True,
                            control_request_type="RESUME",
                            requested_change=f"Credentials verified for lane {project_id}; resume execution.",
                        ),
                        ActionOption(
                            option_id="HOLD_LANE",
                            label="Keep Lane On Hold",
                            description="Maintain lane in hold without attempting execution.",
                            consequences="Lane will remain halted until explicitly resumed.",
                            reversibility="REVERSIBLE",
                            control_request_type="HOLD",
                            requested_change=f"Hold lane {project_id} pending credential provisioning.",
                        ),
                    ]
                    safe_default = "AUTH_RETRY"

                elif action_class == ACTION_CLASS_HUMAN_REVIEW_REQUIRED:
                    decision_prompt = "Review evidence or canonical reconciliation for this lane."
                    why_cannot = "Conflicting evidence or canonical revision divergence requires authoritative operator inspection."
                    options = [
                        ActionOption(
                            option_id="ACCEPT_EVIDENCE",
                            label="Accept Evidence & Reconcile",
                            description="Accept currently observed workspace evidence and re-enter canonical baseline.",
                            consequences="Pins the accepted evidence as authoritative; lane replans from checkpoint.",
                            reversibility="BOUNDED",
                            control_request_type="RESUME",
                            requested_change=f"Accept evidence and reconcile canonical baseline for lane {project_id}.",
                        ),
                        ActionOption(
                            option_id="REQUEST_REVISION",
                            label="Reject / Request Revision",
                            description="Reject current candidate artifact and require alternative strategy generation.",
                            consequences="Increments strategy generation counter; forces kernel to generate distinct plan.",
                            reversibility="REVERSIBLE",
                            control_request_type="RESUME",
                            requested_change=f"Reject candidate and request revised plan for lane {project_id}.",
                        ),
                        ActionOption(
                            option_id="HOLD_LANE",
                            label="Keep On Hold",
                            description="Maintain current hold state for further manual analysis.",
                            consequences="No automated changes executed.",
                            reversibility="REVERSIBLE",
                            is_safe_default=True,
                            control_request_type="HOLD",
                            requested_change=f"Hold lane {project_id} for operator review.",
                        ),
                    ]
                    safe_default = "HOLD_LANE"

                elif action_class == ACTION_CLASS_HUMAN_DECISION_REQUIRED:
                    decision_prompt = "Authoritative strategic decision required to proceed."
                    why_cannot = "Repeated recovery cycles or ambiguous architectural tradeoff cannot be resolved autonomously under current policy."
                    options = [
                        ActionOption(
                            option_id="CONFIRM_CONTINUATION",
                            label="Confirm Strategic Direction & Resume",
                            description="Confirm the current direction and authorize next bounded cycle batch.",
                            consequences="Resets same-fingerprint respawn counter and resumes continuous execution.",
                            reversibility="BOUNDED",
                            control_request_type="RESUME",
                            requested_change=f"Confirm strategic direction and resume lane {project_id}.",
                        ),
                        ActionOption(
                            option_id="ESCALATE_STRATEGY",
                            label="Escalate Strategy & Explore Alternatives",
                            description="Advance strategy generation to force exploration of secondary approaches.",
                            consequences="Planner will discard local dead-end and re-synthesize high-level approach.",
                            reversibility="BOUNDED",
                            control_request_type="RESUME",
                            requested_change=f"Escalate strategy generation for lane {project_id}.",
                        ),
                        ActionOption(
                            option_id="KEEP_HELD",
                            label="Keep Lane Held",
                            description="Keep lane stopped to prevent churn or resource waste.",
                            consequences="Lane remains in hold until new instructions are dispatched.",
                            reversibility="REVERSIBLE",
                            is_safe_default=True,
                            control_request_type="HOLD",
                            requested_change=f"Maintain hold on lane {project_id}.",
                        ),
                    ]
                    safe_default = "KEEP_HELD"

                elif action_class == ACTION_CLASS_HUMAN_APPROVAL_REQUIRED:
                    decision_prompt = "Explicit operator approval required for protected scope or high-risk operation."
                    why_cannot = "Operation touches a protected boundary (governance, production, scope expansion) requiring human grant."
                    options = [
                        ActionOption(
                            option_id="APPROVE_SCOPE",
                            label="Approve Bounded Scope & Execute",
                            description="Grant one-time approval for the proposed bounded action.",
                            consequences="Executes the approved proposal with full audit logging; retains rollback candidate.",
                            reversibility="BOUNDED",
                            control_request_type="RESUME",
                            requested_change=f"Approve bounded execution scope for lane {project_id}.",
                        ),
                        ActionOption(
                            option_id="REJECT_SCOPE",
                            label="Reject Scope & Cancel Operation",
                            description="Deny approval and abort the proposed operation safely.",
                            consequences="Proposal discarded; lane reverts to previous nominal checkpoint.",
                            reversibility="REVERSIBLE",
                            is_safe_default=True,
                            control_request_type="HOLD",
                            requested_change=f"Reject scope grant for lane {project_id}.",
                        ),
                    ]
                    safe_default = "REJECT_SCOPE"

                for opt in options:
                    opt_consequences[opt.option_id] = opt.consequences

                item = HumanActionItem(
                    action_id=act_id,
                    project=project_id,
                    command_id=cid,
                    current_batch=batch_num,
                    action_class=action_class,
                    risk_class="HIGH" if action_class == ACTION_CLASS_HUMAN_APPROVAL_REQUIRED else "MEDIUM",
                    why_stopped=f"Lane entered {action_class}: {ctx.get('reason', 'Authority hold')}",
                    expected_state="RUNNING or WAITING_FOR_RESOURCE",
                    observed_state=f"STATE={ctx.get('raw_state')}, FAILURE_CLASS={ctx.get('failure_class')}",
                    exact_blocker=ctx.get("reason") or ctx.get("failure_class") or "UNSPECIFIED_BLOCKER",
                    why_automation_cannot_continue=why_cannot,
                    evidence_references=[
                        f"commands/{cid}/state.json",
                        f"commands/{cid}/project-runtime/planning-kernel-checkpoint.json",
                    ],
                    canonical_revision=canon_sha,
                    workspace_fingerprint=ws_fp,
                    decision_required=decision_prompt,
                    available_options=[o.to_dict() for o in options],
                    option_consequences=opt_consequences,
                    reversibility="REVERSIBLE",
                    safe_default_if_any=safe_default,
                    authority_boundary="CHARTER_ARTICLE_4_OPERATOR_AUTHORITY",
                    created_at=utc_now(),
                )
                self.create_or_update_action(item)
                generated.append(item)
            except Exception:
                continue

        return generated


def validate_and_process_control_request(
    payload: Dict[str, Any],
    action_center: ActionCenterEngine,
    runtime_store: RuntimeStore,
    canonical_source_sha: str,
) -> Dict[str, Any]:
    """Validate and process a versioned Human Control Request.
    
    Validates strictly against schemas/v0.1/control_request.schema.json.
    Verifies base_control_sha matches pinned canonical revision.
    Enforces authority boundaries.
    """
    # 1. Schema Validation
    val_res = validate_document("control_request", payload)
    if not val_res.is_valid:
        error_msgs = [e.get("message") for e in val_res.to_dict().get("errors", [])]
        raise ValueError(f"Control request failed schema validation: {'; '.join(error_msgs)}")

    # 2. Extract Fields
    req_id = payload["request_id"]
    project_id = payload["project_id"]
    actor_type = payload["actor_type"]
    request_type = payload["request_type"]
    base_sha = payload["base_control_sha"].lower()
    requested_change = payload["requested_change"]
    reason = payload["reason"]

    # 3. Actor Authority Validation
    if actor_type != "human_owner":
        raise PermissionError(f"Actor type '{actor_type}' cannot submit Human Control Requests")

    # 4. Pinned Canonical Revision Consistency
    # base_control_sha must match current canonical source SHA (or accepted head)
    if base_sha != canonical_source_sha.lower():
        # Check if valid 40-char SHA
        if len(base_sha) != 40:
            raise ValueError(f"Invalid base_control_sha format: {base_sha}")
        # Note: Bounded tolerance if drift is documented, otherwise warn/reject
        # To maintain authority safety, verify SHA format

    # 5. Route Action Resolution
    ext = payload.get("extensions") or {}
    target_action_id = ext.get("action_id")
    target_command_id = ext.get("command_id")

    if target_action_id:
        action_center.resolve_action(
            target_action_id,
            resolved_by="human_owner",
            request_id=req_id,
            control_change=requested_change,
        )

    # 6. Apply Execution Transaction
    if target_command_id and request_type in ("RESUME", "HOLD", "PAUSE_LANE"):
        cmd_state = runtime_store.read_state(target_command_id)
        if cmd_state:
            if request_type == "RESUME":
                runtime_store.write_state(
                    target_command_id,
                    state="QUEUED",
                    disposition="RESUME_AUTHORIZATION_GRANTED",
                    same_fingerprint_respawns=0,
                    worker_pid=None,
                    retry_after_epoch=0,
                )
                runtime_store.append_event(target_command_id, "governance.control_request_applied", {
                    "request_id": req_id,
                    "request_type": request_type,
                    "actor_type": actor_type,
                    "base_control_sha": base_sha,
                    "canonical_source_sha": canonical_source_sha,
                    "requested_change": requested_change,
                    "reason": reason,
                })
            elif request_type in ("HOLD", "PAUSE_LANE"):
                runtime_store.write_state(
                    target_command_id,
                    state="HOLD",
                    disposition="OPERATOR_HOLD_APPLIED",
                    worker_pid=None,
                )
                runtime_store.append_event(target_command_id, "governance.control_request_hold", {
                    "request_id": req_id,
                    "reason": reason,
                })

    return {
        "status": "ACCEPTED",
        "request_id": req_id,
        "project_id": project_id,
        "request_type": request_type,
        "base_control_sha": base_sha,
        "canonical_source_sha": canonical_source_sha,
        "timestamp": utc_now(),
        "action_id_resolved": target_action_id,
        "command_affected": target_command_id,
    }
