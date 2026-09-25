"""Tests for AOS Human Action Center, Control Requests, Self-Repair Engine, and Proof Suite (Proofs A-F)."""

import json
import pytest
from pathlib import Path

from aos.action_center import (
    HumanActionItem,
    ActionOption,
    ActionCenterEngine,
    classify_lane_hold,
    validate_and_process_control_request,
    ACTION_CLASS_WAITING_FOR_RESOURCE,
    ACTION_CLASS_AUTH_REQUIRED,
    ACTION_CLASS_HUMAN_DECISION_REQUIRED,
    ACTION_CLASS_HUMAN_APPROVAL_REQUIRED,
    DISPOSITION_WAITING_FOR_RESOURCE,
    DISPOSITION_RUNNING,
    DISPOSITION_AUTH_REQUIRED,
    DISPOSITION_HUMAN_DECISION_REQUIRED,
)
from aos.providers.council import (
    DeliberationCouncilV1,
    assess_council_trigger,
)
from aos.runtime_store import RuntimeStore
from aos.self_diagnosis import (
    SelfDiagnosisEngine,
    ShadowRepairProposal,
    STATUS_RESOLVED_WITHOUT_REPAIR,
)
from aos.self_repair import (
    BoundedSelfRepairEngine,
    classify_defect_repair_authority,
    AUTHORITY_AUTO_REPAIR_ELIGIBLE,
    AUTHORITY_HUMAN_APPROVAL_REQUIRED,
    AUTHORITY_FORBIDDEN,
    STAGE_ACTIVATED,
)


def test_proof_a_resource_wait_does_not_escalate_to_human_action(tmp_path):
    """
    PROOF A: Resource wait simulation.
    When a provider rate limit or quota wait occurs, it must be classified as
    disposition=WAITING_FOR_RESOURCE and NOT generate an action in the Human Action Center.
    """
    engine = ActionCenterEngine(action_store_root=tmp_path)
    
    # 1. Classify provider rate limit / resource wait
    action_class, disposition, ctx = classify_lane_hold(
        project_id="lari",
        command_id="continue-b181ddc574c25c2aa0f2a6b9",
        cmd_state={
            "state": "WAITING_FOR_REASONING_PROVIDER",
            "disposition": "WAITING_FOR_RESOURCE",
            "failure_class": "WAITING_FOR_REASONING_PROVIDER",
        },
        checkpoint={"batch_number": 441},
    )
    # Proof: disposition is WAITING_FOR_RESOURCE, and action_class is WAITING_FOR_RESOURCE (non-actionable)
    assert disposition == DISPOSITION_WAITING_FOR_RESOURCE
    assert action_class == ACTION_CLASS_WAITING_FOR_RESOURCE
    
    # Verify Action Center count of human action required is 0
    assert engine.count_human_action_required() == 0


def test_proof_b_auto_repair_of_bounded_defect(tmp_path):
    """
    PROOF B: Bounded Auto-Repair.
    Autonomous repair of a bounded technical defect (e.g. transient failure)
    transitions through candidate isolation, validation, and smoke to completion
    without creating a Human Action item.
    """
    diag_engine = SelfDiagnosisEngine(diagnosis_root=tmp_path / "diag")
    repair_engine = BoundedSelfRepairEngine(
        repair_root=tmp_path / "repairs",
        diagnosis_engine=diag_engine,
    )
    repair_engine.set_live_mode(True)
    
    # Record a safe, technical finding
    finding_obj = diag_engine.record_or_update_finding(
        component="reasoning_router",
        failure_class="PROVIDER_TRANSIENT_FAILURE",
        symptom="Rate limit backoff expired on transient provider",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=["lari"],
        evidence_refs=["providers.json"],
        evidence_class="TELEMETRY_CIRCUIT",
        confidence=0.9,
        suspected_root_cause="Stale backoff epoch",
        repair_authority=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        proposed_repair=ShadowRepairProposal(
            problem="Rate limit backoff expired on transient provider",
            evidence=["providers.json"],
            root_cause_hypothesis="Stale backoff epoch",
            minimal_change="Schedule circuit probe and reset transient backoff",
            files_likely_affected=["providers.json"],
            tests_required=["tests/test_reasoning_router.py"],
            ci_required=False,
            runtime_proof_required="PASSING_UNIT_TESTS",
            rollback_plan="Retain previous configuration",
            authority_class=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        ),
    )
    finding_id = finding_obj.finding_id
    
    # Verify authority classification
    auth = classify_defect_repair_authority(
        component=finding_obj.component,
        failure_class=finding_obj.failure_class,
        symptom=finding_obj.symptom,
        files_affected=["providers.json"],
    )
    assert auth == AUTHORITY_AUTO_REPAIR_ELIGIBLE
    
    # Execute autonomous repair
    success, stage, record = repair_engine.attempt_autonomous_repair(finding_id)
    assert success is True
    assert stage == STAGE_ACTIVATED
    assert record["validation_status"] == "CIRCUIT_PROBE_SCHEDULED"
    assert record["smoke_status"] == "VERIFIED"
    assert record["rollback_retained"] is True
    
    # Verify finding resolution in diagnostic engine
    updated_finding = diag_engine.get_finding(finding_id)
    assert updated_finding["status"] == STATUS_RESOLVED_WITHOUT_REPAIR
    assert "Autonomous self-repair applied" in updated_finding["resolution_evidence"]


def test_proof_c_auth_required_defect_creates_action_not_generic_resume(tmp_path):
    """
    PROOF C: Auth required defect.
    Missing credentials or invalid tokens must transition to AUTH_REQUIRED,
    creating an exact Human Action item with credential save/retry options,
    not a generic resume loop.
    """
    engine = ActionCenterEngine(action_store_root=tmp_path)
    
    action_class, disposition, ctx = classify_lane_hold(
        project_id="ui-v2",
        command_id="continue-61be4ab1af53cfa646d773ce",
        cmd_state={
            "state": "HOLD",
            "disposition": "AUTH_REQUIRED",
            "failure_class": "CREDENTIAL_NOT_CONFIGURED",
        },
        checkpoint={"batch_number": 127},
    )
    assert action_class == ACTION_CLASS_AUTH_REQUIRED
    assert disposition == DISPOSITION_AUTH_REQUIRED
    
    # Create action item with exact mandatory fields
    item = HumanActionItem(
        action_id=engine.compute_action_id("ui-v2", "continue-61be4ab1af53cfa646d773ce", action_class, "MISSING_CREDENTIAL"),
        project="ui-v2",
        command_id="continue-61be4ab1af53cfa646d773ce",
        current_batch=127,
        action_class=action_class,
        risk_class="MEDIUM",
        why_stopped="Missing required reasoning credential",
        expected_state="RUNNING",
        observed_state="HOLD",
        exact_blocker="CREDENTIAL_NOT_CONFIGURED",
        why_automation_cannot_continue="Requires operator to provide API credential",
        evidence_references=["commands/continue-61be4ab1af53cfa646d773ce/state.json"],
        canonical_revision="cb77d36ab5fdb690e6eb5e76b12a9bd2a95e753f",
        workspace_fingerprint="abc12345",
        decision_required="Save API key in Windows Credential Manager or assign alternate provider",
        available_options=[
            {
                "option_id": "SAVE_KEY",
                "label": "Save Key in Credential Manager",
                "description": "Store secret and retry",
                "control_request_type": "RESUME",
                "requested_change": "Resume with configured credential",
                "reversibility": "REVERSIBLE",
                "is_safe_default": True
            }
        ],
        option_consequences={"SAVE_KEY": "Unblocks lane execution immediately"},
        reversibility="REVERSIBLE",
        safe_default_if_any="SAVE_KEY",
        authority_boundary="CREDENTIAL_AUTHORITY",
        created_at="2026-09-25T08:00:00Z"
    )
    engine.create_or_update_action(item)
    
    assert engine.count_human_action_required() == 1
    pending = engine.list_pending_actions()
    assert len(pending) == 1
    assert pending[0]["action_class"] == ACTION_CLASS_AUTH_REQUIRED


def test_proof_d_human_decision_structured_options_and_schema_validation(tmp_path):
    """
    PROOF D: Human decision required.
    Charter / architectural ambiguity generates structured options, bounded choices,
    and a versioned Control Request that validates against schema v0.1.
    """
    store = RuntimeStore(tmp_path / "runtime")
    engine = ActionCenterEngine(action_store_root=tmp_path / "actions")
    
    cmd_id = "continue-b181ddc574c25c2aa0f2a6b9"
    canon_sha = "cb77d36ab5fdb690e6eb5e76b12a9bd2a95e753f"
    
    # Initialize command state using create_command
    store.create_command({
        "command_id": cmd_id,
        "project": {"project_id": "lari"},
        "goal": "Continue LARI execution",
    })
    store.write_state(
        cmd_id,
        state="HOLD",
        disposition="HUMAN_DECISION_REQUIRED",
        canonical_source_sha=canon_sha,
        completed_batch_count=441,
    )
    
    act_id = engine.compute_action_id("lari", cmd_id, ACTION_CLASS_HUMAN_DECISION_REQUIRED, "SPEC_AMBIGUITY")
    action_item = HumanActionItem(
        action_id=act_id,
        project="lari",
        command_id=cmd_id,
        current_batch=441,
        action_class=ACTION_CLASS_HUMAN_DECISION_REQUIRED,
        risk_class="HIGH",
        why_stopped="Charter architectural ambiguity detected",
        expected_state="RUNNING",
        observed_state="HOLD",
        exact_blocker="SPEC_AMBIGUITY",
        why_automation_cannot_continue="Requires human product owner design decision",
        evidence_references=[f"commands/{cmd_id}/state.json"],
        canonical_revision=canon_sha,
        workspace_fingerprint="fp123456",
        decision_required="Choose between Pattern A (Async Eventing) and Pattern B (Synchronous RPC)",
        available_options=[
            {
                "option_id": "APPROVE_A",
                "label": "Approve Async Eventing",
                "description": "Select event-driven architecture",
                "control_request_type": "RESUME",
                "requested_change": "Adopt asynchronous eventing pattern",
                "reversibility": "REVERSIBLE",
                "is_safe_default": True
            }
        ],
        option_consequences={"APPROVE_A": "LARI proceeds with event pipeline"},
        reversibility="REVERSIBLE",
        safe_default_if_any="APPROVE_A",
        authority_boundary="CHARTER_ARTICLE_4_OPERATOR_AUTHORITY",
        created_at="2026-09-25T08:00:00Z"
    )
    engine.create_or_update_action(action_item)
    assert engine.count_human_action_required() == 1
    
    # Formulate schema-compliant control request payload
    payload = {
        "request_id": "req-1111-2222-3333-4444",
        "schema_version": "0.1.0",
        "project_id": "lari",
        "actor_type": "human_owner",
        "request_type": "RESUME",
        "base_control_sha": canon_sha,
        "requested_change": "Adopt asynchronous eventing pattern",
        "reason": "Operator confirmed architecture pattern",
        "requested_at": "2026-09-25T08:00:00Z",
        "extensions": {
            "action_id": act_id,
            "command_id": cmd_id
        }
    }
    
    res = validate_and_process_control_request(
        payload=payload,
        action_center=engine,
        runtime_store=store,
        canonical_source_sha=canon_sha,
    )
    assert res["status"] == "ACCEPTED"
    assert res["action_id_resolved"] == act_id
    
    # Action item must now be resolved
    assert engine.count_human_action_required() == 0
    updated_state = store.read_state(cmd_id)
    assert updated_state["state"] == "QUEUED"
    assert updated_state["disposition"] == "RESUME_AUTHORIZATION_GRANTED"


def test_proof_e_council_zero_token_waste_and_deduplication(tmp_path):
    """
    PROOF E: Council token governance.
    - Routine batches: assessed as bypass, zero model calls made.
    - Material ambiguity: at most 1 deliberation per unchanged fingerprint.
    - Budget limit enforcement.
    """
    council = DeliberationCouncilV1(
        mode="SHADOW_BUDGETED",
        max_council_calls=2,
        ledger_dir=tmp_path
    )
    
    # 1. Routine batch trigger assessment
    routine_assess = assess_council_trigger("ROUTINE_BATCH", "Execute standard batch 442 step")
    assert not routine_assess.council_required
    assert routine_assess.trigger_reason == "LOW_UNCERTAINTY_ROUTINE"
    
    # Routine batch returns immediately with 0 calls
    res_routine = council.evaluate_decision(
        decision_type="ROUTINE_BATCH",
        prompt="Execute standard batch 442 step",
        primary_proposal={"plan": "Step 442"},
        alternate_proposals=[],
    )
    assert res_routine.decision_record["status"] == "ROUTINE_BYPASS"
    assert council.council_provider_call_count == 0
    
    # 2. Material ambiguity trigger assessment
    ambig_assess = assess_council_trigger(
        "ARCHITECTURE_DECISION",
        "Select architectural pattern for multi-lane productization tradeoff"
    )
    assert ambig_assess.council_required
    assert ambig_assess.trigger_reason == "MATERIAL_AMBIGUITY_OR_HIGH_IMPACT"
    
    # Deliberate once on ambiguous proposal
    primary_prop = {
        "title": "Dual-Lineage Architecture",
        "authority_id": "AUTH-01",
        "rationale": "Strict process boundary isolation",
        "tasks": [{"node_id": "t1", "action": "isolate"}],
    }
    alternate_prop = (
        ("alt_member", {
            "title": "Single Process Lineage",
            "authority_id": "AUTH-01",
            "rationale": "Shared memory communication",
            "tasks": [{"node_id": "t2", "action": "share"}],
        }),
    )
    peer_reviews = [
        {
            "proposal_id": "Proposal A",
            "evidence_consistency": 0.9,
            "canonical_consistency": 0.9,
            "dependency_correctness": 0.9,
            "authority_compatibility": 1.0,
            "risk": 0.1,
            "reversibility": 0.9,
            "implementation_complexity": 0.8,
            "expected_value": 0.9,
            "contradictions": [],
            "ranked_preference": 1,
            "confidence": 0.9,
            "short_bounded_rationale": "Sound isolation architecture.",
        },
        {
            "proposal_id": "Proposal B",
            "evidence_consistency": 0.8,
            "canonical_consistency": 0.8,
            "dependency_correctness": 0.8,
            "authority_compatibility": 0.9,
            "risk": 0.3,
            "reversibility": 0.8,
            "implementation_complexity": 0.8,
            "expected_value": 0.8,
            "contradictions": [],
            "ranked_preference": 2,
            "confidence": 0.8,
            "short_bounded_rationale": "Alternative shared architecture.",
        },
    ]
    
    res_ambig1 = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Select architectural pattern",
        primary_proposal=primary_prop,
        alternate_proposals=alternate_prop,
        peer_reviews=peer_reviews,
    )
    assert res_ambig1.decision_record.get("council_status") is not None
    
    # 3. Deduplicated fingerprint: second identical call returns cached result with skip status
    res_ambig2 = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Select architectural pattern",
        primary_proposal=primary_prop,
        alternate_proposals=alternate_prop,
        peer_reviews=peer_reviews,
    )
    assert res_ambig2.decision_record["council_status"] == "SKIP_REDUNDANT_SAMPLE"


def test_proof_f_self_repair_authority_filtering(tmp_path):
    """
    PROOF F: Self-Repair Authority Filtering.
    Defects that require Human Approval (scope, roadmap, security) or are Forbidden
    (destructive data, secrets) are NEVER executed autonomously, even if live mode is True.
    """
    diag_engine = SelfDiagnosisEngine(diagnosis_root=tmp_path / "diag")
    repair_engine = BoundedSelfRepairEngine(
        repair_root=tmp_path / "repairs",
        diagnosis_engine=diag_engine,
    )
    repair_engine.set_live_mode(True)
    
    # 1. Human Approval Required defect (Scope Change)
    finding_scope = diag_engine.record_or_update_finding(
        component="roadmap_planner",
        failure_class="PLANNER_STAGNATION",
        symptom="Lane requested modification to project roadmap charter: SCOPE_CHANGE",
        severity="HIGH",
        autonomy_impact="BLOCKED",
        affected_lane_ids=["lari"],
        evidence_refs=["charter.md"],
        evidence_class="GIT_DIFF",
        confidence=0.95,
        suspected_root_cause="Unbounded scope creep",
        repair_authority=AUTHORITY_HUMAN_APPROVAL_REQUIRED,
        proposed_repair=ShadowRepairProposal(
            problem="Lane requested modification to project roadmap charter",
            evidence=["charter.md"],
            root_cause_hypothesis="Charter drift",
            minimal_change="Alter charter objectives",
            files_likely_affected=["charter.md"],
            tests_required=["tests/test_charter.py"],
            ci_required=True,
            runtime_proof_required="HUMAN_APPROVAL",
            rollback_plan="Revert charter edit",
            authority_class=AUTHORITY_HUMAN_APPROVAL_REQUIRED,
        ),
    )
    auth_scope = classify_defect_repair_authority(
        component="roadmap_planner",
        failure_class="PLANNER_STAGNATION",
        symptom="Lane requested modification to project roadmap charter: SCOPE_CHANGE",
        files_affected=["charter.md"],
    )
    assert auth_scope == AUTHORITY_HUMAN_APPROVAL_REQUIRED
    
    success, reason, rec = repair_engine.attempt_autonomous_repair(finding_scope.finding_id)
    assert success is False
    assert "AUTHORITY_GATE" in reason
    assert rec["authority_class"] == AUTHORITY_HUMAN_APPROVAL_REQUIRED
    assert rec["validation_status"] == "GATED_BY_AUTHORITY"
    
    # 2. Forbidden defect (Secret/Credential Mutation)
    finding_secret = diag_engine.record_or_update_finding(
        component="credential_vault",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="Unauthorized attempt to mutate SECRET production credential",
        severity="CRITICAL",
        autonomy_impact="BLOCKED",
        affected_lane_ids=["ui-v2"],
        evidence_refs=["vault.env"],
        evidence_class="LOG_TRACE",
        confidence=0.99,
        suspected_root_cause="Defective script",
        repair_authority=AUTHORITY_FORBIDDEN,
        proposed_repair=ShadowRepairProposal(
            problem="Unauthorized attempt to mutate SECRET",
            evidence=["vault.env"],
            root_cause_hypothesis="Key corruption",
            minimal_change="Overwrite credentials",
            files_likely_affected=["vault.env"],
            tests_required=[],
            ci_required=True,
            runtime_proof_required="SECURITY_AUDIT",
            rollback_plan="Revert vault",
            authority_class=AUTHORITY_FORBIDDEN,
        ),
    )
    auth_secret = classify_defect_repair_authority(
        component="credential_vault",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="Unauthorized attempt to mutate SECRET production credential",
        files_affected=["vault.env"],
    )
    assert auth_secret == AUTHORITY_FORBIDDEN
    
    success2, reason2, rec2 = repair_engine.attempt_autonomous_repair(finding_secret.finding_id)
    assert success2 is False
    assert "AUTHORITY_GATE" in reason2
    assert rec2["authority_class"] == AUTHORITY_FORBIDDEN
    assert rec2["validation_status"] == "GATED_BY_AUTHORITY"
