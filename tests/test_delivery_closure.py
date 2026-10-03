"""Unit and regression tests for AOS Delivery Closure and Standing Autonomy."""
import json
import subprocess
import time
from pathlib import Path
import pytest

from aos.delivery_state import (DeliveryStage, DeliveryState, advance_delivery_stage, read_delivery_state, write_delivery_state)
from aos.acceptance_receipt import (AcceptanceReceipt, read_latest_acceptance_receipt, write_acceptance_receipt)
from aos.runtime_admission import (AdmissionRecord, AdmissionState, CommandAdmissionStore)
from aos.runtime_worker import build_recovery_fingerprint
from aos.canonical_reconciler import (validate_canonical_coherence, CanonicalReconciliationError)
from aos.cross_lane_coordinator import evaluate_downstream_gates


def test_delivery_state_forward_only(tmp_path: Path):
    runtime_dir = tmp_path / 'project-runtime'
    runtime_dir.mkdir(parents=True)
    
    st = DeliveryState(
        delivery_id='deliv-01',
        objective_id='obj-01',
        execution_base_sha='814e3ca0c09c3a484e20869f1a47a3545259f6db',
        candidate_sha=None,
        stage=DeliveryStage.PREPARING.value,
        ci_workflow_identity='lari-ci.yml',
        ci_run_id=None,
        ci_conclusion=None,
        ci_attempt_count=0,
        push_target_branch='main',
        created_at='2026-10-03T12:00:00Z',
        updated_at='2026-10-03T12:00:00Z',
        production='NO_GO',
    )
    write_delivery_state(runtime_dir, st)
    
    read_st = read_delivery_state(runtime_dir)
    assert read_st is not None
    assert read_st.stage == DeliveryStage.PREPARING.value
    assert read_st.production == 'NO_GO'

    # Advance stage
    updated = advance_delivery_stage(runtime_dir, DeliveryStage.COMMITTED, candidate_sha='a'*40)
    assert updated.stage == DeliveryStage.COMMITTED.value
    assert updated.candidate_sha == 'a'*40

    # Cannot advance backwards
    with pytest.raises(ValueError):
        advance_delivery_stage(runtime_dir, DeliveryStage.PREPARING)


def test_delivery_progress_changes_fingerprint():
    state1 = {'state': 'TECHNICAL_HOLD', 'objective_id': 'obj-1'}
    fp1 = build_recovery_fingerprint(project_id='lari', state=state1, checkpoint={})

    state2 = {'state': 'TECHNICAL_HOLD', 'objective_id': 'obj-1', 'candidate_sha': 'a'*40, 'delivery_stage': 'COMMITTED'}
    fp2 = build_recovery_fingerprint(project_id='lari', state=state2, checkpoint={})

    assert fp1 != fp2


def test_acceptance_receipt_roundtrip(tmp_path: Path):
    control_dir = tmp_path / 'control'
    receipt = AcceptanceReceipt(
        receipt_id='receipt-01',
        project_id='lari',
        lane='lari',
        slice_id='phase7-node2-r2',
        execution_base_sha='814e3ca0c09c3a484e20869f1a47a3545259f6db',
        candidate_sha='9999999999999999999999999999999999999999',
        ci_workflow_name='lari-ci.yml',
        ci_run_id=12345,
        ci_conclusion='success',
        acceptance_result='ACCEPTED',
        controller_authority='DECISION-022',
        canonical_control_transition_sha='c'*40,
        created_at='2026-10-03T12:00:00Z',
        production='NO_GO',
    )
    p = write_acceptance_receipt(control_dir, receipt)
    assert p.is_file()

    read_back = read_latest_acceptance_receipt(control_dir, 'lari')
    assert read_back is not None
    assert read_back.candidate_sha == '9999999999999999999999999999999999999999'
    assert read_back.slice_id == 'phase7-node2-r2'


def test_canonical_coherence_fails_closed_on_drift():
    incoherent = {
        'current_status': 'PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_ADAPTER_IN_PROGRESS',
        'current_milestone': 'Node 2 Discovery Marketplace R2',
        'next_action': 'Implement Phase 7 Node 2 R1 contracts',
    }
    with pytest.raises(CanonicalReconciliationError):
        validate_canonical_coherence(incoherent)

    coherent = {
        'current_status': 'PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R2_ADAPTER_IN_PROGRESS',
        'current_milestone': 'Node 2 Discovery Marketplace R2',
        'next_action': 'Implement Phase 7 Node 2 R2 typed adapter and verify contracts',
    }
    validate_canonical_coherence(coherent)


def test_system_defect_reactivation(tmp_path: Path):
    store = CommandAdmissionStore(tmp_path)
    cmd_dir = tmp_path / 'commands' / 'continue-b181ddc574c25c2aa0f2a6b9'
    cmd_dir.mkdir(parents=True)
    (cmd_dir / 'command.json').write_text(json.dumps({'project': {'project_id': 'lari'}}), encoding='utf-8')
    (cmd_dir / 'state.json').write_text(json.dumps({'state': 'HUMAN_REQUIRED', 'failure_class': 'RECOVERY_CHURN_GUARD'}), encoding='utf-8')

    with pytest.raises(ValueError):
        store.activate_system_defect_remediated(
            'continue-b181ddc574c25c2aa0f2a6b9',
            defect_class='UNKNOWN_DEFECT',
            repaired_runtime_sha='63bf4e30db6ddbf36fd015a5ddfef7fdad61c9fa',
            prior_terminal_state='HUMAN_REQUIRED',
            prior_failure_class='UNKNOWN_DEFECT',
            canonical_authority_valid=True,
            production='NO_GO',
        )

    with pytest.raises(ValueError):
        store.activate_system_defect_remediated(
            'continue-b181ddc574c25c2aa0f2a6b9',
            defect_class='RECOVERY_CHURN_GUARD',
            repaired_runtime_sha='63bf4e30db6ddbf36fd015a5ddfef7fdad61c9fa',
            prior_terminal_state='HUMAN_REQUIRED',
            prior_failure_class='RECOVERY_CHURN_GUARD',
            canonical_authority_valid=True,
            production='GO',
        )

    rec = store.activate_system_defect_remediated(
        'continue-b181ddc574c25c2aa0f2a6b9',
        defect_class='RECOVERY_CHURN_GUARD',
        repaired_runtime_sha='63bf4e30db6ddbf36fd015a5ddfef7fdad61c9fa',
        prior_terminal_state='HUMAN_REQUIRED',
        prior_failure_class='RECOVERY_CHURN_GUARD',
        canonical_authority_valid=True,
        production='NO_GO',
    )
    assert rec.state == AdmissionState.ACTIVE.value
    assert rec.authority == 'SYSTEM_DEFECT_REMEDIATED'
    assert rec.recovery_proof_id is None

    st = json.loads((cmd_dir / 'state.json').read_text(encoding='utf-8'))
    assert st['state'] == 'RECOVERING'
    assert st['disposition'] == 'SYSTEM_DEFECT_REMEDIATED'


def test_ui_v2_downstream_gate(tmp_path: Path):
    store = CommandAdmissionStore(tmp_path)
    ui_cmd_id = 'continue-61be4ab1af53cfa646d773ce'
    cmd_dir = tmp_path / 'commands' / ui_cmd_id
    cmd_dir.mkdir(parents=True)
    (cmd_dir / 'command.json').write_text(json.dumps({'project': {'project_id': 'lari'}}), encoding='utf-8')

    control_dir = tmp_path / 'control'
    control_pc = control_dir / 'docs' / 'project-control'
    control_pc.mkdir(parents=True)
    
    (control_pc / 'STATE.json').write_text(json.dumps({'current_status': 'R1_ACCEPTED'}), encoding='utf-8')
    res = evaluate_downstream_gates(control_dir, store)
    assert len(res) == 0
    assert store.get(ui_cmd_id).state == AdmissionState.HOLD.value

    (control_pc / 'STATE.json').write_text(json.dumps({
        'current_status': 'PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY',
        'current_milestone': 'Program V2 Phase 7 — Node 2 Discovery Marketplace R3',
        'next_action': 'Implement Phase 7 Node 2 R3',
    }), encoding='utf-8')
    res2 = evaluate_downstream_gates(control_dir, store)
    assert len(res2) == 1
    assert res2[0].command_id == ui_cmd_id
    assert res2[0].state == AdmissionState.ACTIVE.value
    assert res2[0].authority == 'DOWNSTREAM_GATE_SATISFIED'