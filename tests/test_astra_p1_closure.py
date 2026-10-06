"""Targeted unit tests for Astra P1 defects closure:
1. ACCEPTED Frontier Safety
2. Typed Acceptance Safety
3. Hold Semantics
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from aos import acceptance_receipt, canonical_reconciler as cr, cross_lane_coordinator
from aos.acceptance_receipt import AcceptanceReceipt, read_latest_acceptance_receipt, write_acceptance_receipt
from aos.canonical_reconciler import (
    CanonicalReconciliationError,
    derive_latest_accepted_product_sha,
    record_combined_release_acceptance,
    record_lari_ui_v2_acceptance,
    record_phase7_node3_r2_acceptance,
    record_program_v2_convergence_acceptance,
    record_slice_acceptance,
)
from aos.cross_lane_coordinator import evaluate_downstream_gates
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt
from aos.runtime_admission import AdmissionRecord, AdmissionState, CommandAdmissionStore


def _git_commit(repo: Path, filename: str, content: str) -> str:
    f = repo / filename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=AOS Test", "-c", "user.email=aos@example.invalid", "commit", "-m", f"add {filename}"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True)
    return res.stdout.strip().lower()


from aos.knowledge.ingress import ingest_accepted_work


def _accepted_work_ledger(tmp_path: Path, result_sha: str) -> KnowledgeLedger:
    ledger = KnowledgeLedger(tmp_path / f"kcp-{result_sha[:8]}")
    ingest_accepted_work(
        ledger,
        project_id="lari",
        agent_class="CODEX",
        tool_name="pytest",
        base_sha="a" * 40,
        result_sha=result_sha.lower(),
        repository="MertSGI/Randapp-main",
        branch="test/accepted-work",
        module_ids=["acceptance"],
        changed_paths=["product/candidate.py"],
        evidence_refs=["ci-run:123"],
        verification_status="SUCCESS",
        canonical_next_action="Canonical acceptance",
        idempotency_key=f"accepted-{result_sha[:8]}",
    )
    return ledger



# ==============================================================================
# 1. ACCEPTED FRONTIER SAFETY TESTS
# ==============================================================================

def test_rejected_receipt_cannot_become_frontier(tmp_path: Path):
    """A receipt with acceptance_result == REJECTED must not be selected as accepted frontier."""
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init"], cwd=str(product), check=True, capture_output=True)
    cand_sha = _git_commit(product, "code.txt", "v1")

    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["git", "init"], cwd=str(control), check=True, capture_output=True)

    receipt = AcceptanceReceipt(
        receipt_id="receipt-rejected",
        project_id="lari",
        lane="lane-b",
        slice_id="test_slice",
        execution_base_sha="0" * 40,
        candidate_sha=cand_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=1,
        ci_conclusion="failure",
        acceptance_result="REJECTED",
        controller_authority="AUTH-1",
        control_sha_before="1" * 40,
    )
    write_acceptance_receipt(control, receipt)

    # Calling read_latest_acceptance_receipt with require_accepted=True returns None
    assert read_latest_acceptance_receipt(control, require_accepted=True) is None

    # derive_latest_accepted_product_sha fails closed rather than picking the rejected receipt
    with pytest.raises(CanonicalReconciliationError, match="No valid ACCEPTED structured acceptance receipts found"):
        derive_latest_accepted_product_sha(control, product, project_id="lari", lane="lane-b")


def test_wrong_project_receipt_rejected(tmp_path: Path):
    """Receipt for a different project is rejected when project_id is bound."""
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init"], cwd=str(product), check=True, capture_output=True)
    cand_sha = _git_commit(product, "code.txt", "v1")

    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["git", "init"], cwd=str(control), check=True, capture_output=True)

    receipt = AcceptanceReceipt(
        receipt_id="receipt-other",
        project_id="other_project",
        lane="lane-b",
        slice_id="test_slice",
        execution_base_sha="0" * 40,
        candidate_sha=cand_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=1,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-1",
        control_sha_before="1" * 40,
    )
    write_acceptance_receipt(control, receipt)

    # Filtered out by read_latest_acceptance_receipt
    assert read_latest_acceptance_receipt(control, project_id="lari") is None

    # derive_latest_accepted_product_sha fails closed if called for project 'lari'
    with pytest.raises(CanonicalReconciliationError):
        derive_latest_accepted_product_sha(control, product, project_id="lari")


def test_wrong_lane_receipt_rejected(tmp_path: Path):
    """Receipt for a different lane is rejected when lane is bound."""
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init"], cwd=str(product), check=True, capture_output=True)
    cand_sha = _git_commit(product, "code.txt", "v1")

    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["git", "init"], cwd=str(control), check=True, capture_output=True)

    receipt = AcceptanceReceipt(
        receipt_id="receipt-lane-a",
        project_id="lari",
        lane="lane-a",
        slice_id="test_slice",
        execution_base_sha="0" * 40,
        candidate_sha=cand_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=1,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-1",
        control_sha_before="1" * 40,
    )
    write_acceptance_receipt(control, receipt)

    assert read_latest_acceptance_receipt(control, project_id="lari", lane="lane-b") is None

    with pytest.raises(CanonicalReconciliationError):
        derive_latest_accepted_product_sha(control, product, project_id="lari", lane="lane-b")


def test_ambiguous_frontier_fails_closed(tmp_path: Path):
    """If two independent candidate receipts exist without linear succession, fail closed."""
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init"], cwd=str(product), check=True, capture_output=True)
    base_sha = _git_commit(product, "base.txt", "base")
    cand1 = _git_commit(product, "branch1.txt", "b1")
    # reset to base to create branching history
    subprocess.run(["git", "checkout", base_sha], cwd=str(product), check=True, capture_output=True)
    cand2 = _git_commit(product, "branch2.txt", "b2")

    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["git", "init"], cwd=str(control), check=True, capture_output=True)

    # Two receipts both branching off base_sha
    r1 = AcceptanceReceipt(
        receipt_id="receipt-slice1",
        project_id="lari",
        lane="lane-b",
        slice_id="slice1",
        execution_base_sha=base_sha,
        candidate_sha=cand1,
        ci_workflow_name="ci.yml",
        ci_run_id=1,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-1",
        control_sha_before="1" * 40,
        created_at="2026-10-06T10:00:00Z",
    )
    r2 = AcceptanceReceipt(
        receipt_id="receipt-slice2",
        project_id="lari",
        lane="lane-b",
        slice_id="slice2",
        execution_base_sha=base_sha,
        candidate_sha=cand2,
        ci_workflow_name="ci.yml",
        ci_run_id=2,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-2",
        control_sha_before="1" * 40,
        created_at="2026-10-06T11:00:00Z",
    )
    write_acceptance_receipt(control, r1)
    write_acceptance_receipt(control, r2)

    with pytest.raises(CanonicalReconciliationError, match="AMBIGUOUS_ACCEPTED_FRONTIER"):
        derive_latest_accepted_product_sha(control, product, project_id="lari", lane="lane-b")


def test_valid_accepted_canonical_successor_selected(tmp_path: Path):
    """Linear succession R1 -> R2 correctly selects terminal R2 even if timestamp sorting is tested."""
    product = tmp_path / "product"
    product.mkdir()
    subprocess.run(["git", "init"], cwd=str(product), check=True, capture_output=True)
    base_sha = _git_commit(product, "base.txt", "base")
    cand1 = _git_commit(product, "r1.txt", "r1")
    cand2 = _git_commit(product, "r2.txt", "r2")

    control = tmp_path / "control"
    control.mkdir()
    subprocess.run(["git", "init"], cwd=str(control), check=True, capture_output=True)

    # R1 from base -> cand1, R2 from cand1 -> cand2
    r1 = AcceptanceReceipt(
        receipt_id="receipt-r1",
        project_id="lari",
        lane="lane-b",
        slice_id="discovery_marketplace_r1",
        execution_base_sha=base_sha,
        candidate_sha=cand1,
        ci_workflow_name="ci.yml",
        ci_run_id=1,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-1",
        control_sha_before="1" * 40,
        created_at="2026-10-06T10:00:00Z",
    )
    r2 = AcceptanceReceipt(
        receipt_id="receipt-r2",
        project_id="lari",
        lane="lane-b",
        slice_id="discovery_marketplace_r2",
        execution_base_sha=cand1,
        candidate_sha=cand2,
        ci_workflow_name="ci.yml",
        ci_run_id=2,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="AUTH-2",
        control_sha_before="2" * 40,
        created_at="2026-10-06T11:00:00Z",
    )
    write_acceptance_receipt(control, r1)
    write_acceptance_receipt(control, r2)

    sha, rel_path, rank = derive_latest_accepted_product_sha(control, product, project_id="lari", lane="lane-b")
    assert sha == cand2
    assert "acceptance-receipt-discovery_marketplace_r2.json" in rel_path


# ==============================================================================
# 2. TYPED ACCEPTANCE SAFETY TESTS
# ==============================================================================

def test_generic_node2_acceptance_cannot_process_node3_or_ui_or_convergence(tmp_path: Path):
    """record_slice_acceptance fails closed when invoked for Node 3, UI, or convergence slices."""
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps({"project_id": "lari", "repository": "test/repo", "control_ref": "main"}))

    invalid_slices = ["phase7_node3_r1", "lari_ui_v2", "program_v2_convergence", "combined_release"]
    for slice_id in invalid_slices:
        with pytest.raises(CanonicalReconciliationError, match="reserved exclusively for Node 2 delivery slices"):
            record_slice_acceptance(
                descriptor_path=descriptor,
                product_workspace=tmp_path / "product",
                runtime_dir=tmp_path / "runtime",
                candidate_sha="a" * 40,
                execution_base_sha="b" * 40,
                ci_evidence={"run_id": 123},
                slice_id=slice_id,
                acceptance_receipt=None,
            )


def test_typed_transition_rejects_wrong_predecessor_lane_ci_and_kcp(tmp_path: Path, monkeypatch):
    """Each typed transition rejects wrong predecessor authority, lane, missing CI, missing KCP."""
    descriptor = tmp_path / "lari.json"
    descriptor.write_text(json.dumps({
        "project_id": "lari",
        "repository": "MertSGI/Randapp-main",
        "control_ref": "control/lari-project-control-plane",
    }))

    candidate_sha = "c" * 40
    base_sha = "b" * 40
    control_before = "d" * 40

    control = tmp_path / "control"
    state_path = control / "docs" / "project-control" / "STATE.json"
    state_path.parent.mkdir(parents=True)
    state = {
        "current_status": "READY",
        "current_milestone": "Testing",
        "next_action": "Do something",
        "next_action_execution_base_sha": base_sha,
        "candidate_release": {"accepted_product_sha": base_sha},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(cr, "_ensure_control_clone", lambda *args: (control, control_before))
    monkeypatch.setattr(cr, "_git", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "docs/project-control/STATE.json\n", ""))

    ledger = _accepted_work_ledger(tmp_path, candidate_sha)

    # 1. Reject missing / wrong CI
    bad_receipt = AcceptanceReceipt(
        receipt_id="typed-test",
        project_id="lari",
        lane="convergence",
        slice_id="program_v2_convergence",
        execution_base_sha=base_sha,
        candidate_sha=candidate_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=999,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="CONTROLLER-AUTH-1",
        control_sha_before=control_before,
    )
    with pytest.raises(CanonicalReconciliationError, match="(Hosted CI run|Could not independently read GitHub Actions run)"):
        record_program_v2_convergence_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=candidate_sha,
            execution_base_sha=base_sha,
            ci_evidence={"run_id": 999, "status": "completed", "conclusion": "failure", "head_sha": candidate_sha},
            acceptance_receipt=bad_receipt,
            expected_predecessor_authority="CONTROLLER-AUTH-1",
            knowledge_ledger=ledger,
        )

    # 2. Reject wrong lane in receipt
    wrong_lane_receipt = AcceptanceReceipt(
        receipt_id="typed-test",
        project_id="lari",
        lane="lane-b",  # Convergence expects "convergence"
        slice_id="program_v2_convergence",
        execution_base_sha=base_sha,
        candidate_sha=candidate_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="CONTROLLER-AUTH-1",
        control_sha_before=control_before,
    )
    with pytest.raises(CanonicalReconciliationError, match="lane mismatch"):
        record_program_v2_convergence_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=candidate_sha,
            execution_base_sha=base_sha,
            ci_evidence={"run_id": 123, "status": "completed", "conclusion": "success", "head_sha": candidate_sha},
            acceptance_receipt=wrong_lane_receipt,
            expected_predecessor_authority="CONTROLLER-AUTH-1",
            knowledge_ledger=ledger,
        )

    # 3. Reject wrong predecessor authority
    wrong_auth_receipt = AcceptanceReceipt(
        receipt_id="typed-test",
        project_id="lari",
        lane="convergence",
        slice_id="program_v2_convergence",
        execution_base_sha=base_sha,
        candidate_sha=candidate_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="UNAUTHORIZED_ENTITY",
        control_sha_before=control_before,
    )
    with pytest.raises(CanonicalReconciliationError, match="Controller authority mismatch"):
        record_program_v2_convergence_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=candidate_sha,
            execution_base_sha=base_sha,
            ci_evidence={"run_id": 123, "status": "completed", "conclusion": "success", "head_sha": candidate_sha},
            acceptance_receipt=wrong_auth_receipt,
            expected_predecessor_authority="CONTROLLER-AUTH-1",
            knowledge_ledger=ledger,
        )

    # 4. Reject missing KCP exact-SHA coverage
    valid_receipt = AcceptanceReceipt(
        receipt_id="typed-test",
        project_id="lari",
        lane="convergence",
        slice_id="program_v2_convergence",
        execution_base_sha=base_sha,
        candidate_sha=candidate_sha,
        ci_workflow_name="ci.yml",
        ci_run_id=123,
        ci_conclusion="success",
        acceptance_result="ACCEPTED",
        controller_authority="CONTROLLER-AUTH-1",
        control_sha_before=control_before,
    )
    empty_ledger = KnowledgeLedger(tmp_path / "empty-kcp")
    with pytest.raises(CanonicalReconciliationError, match="KCP_ACCEPTED_WORK_COVERAGE_REQUIRED"):
        record_program_v2_convergence_acceptance(
            descriptor_path=descriptor,
            product_workspace=tmp_path / "product",
            runtime_dir=tmp_path / "runtime",
            candidate_sha=candidate_sha,
            execution_base_sha=base_sha,
            ci_evidence={"run_id": 123, "status": "completed", "conclusion": "success", "head_sha": candidate_sha},
            acceptance_receipt=valid_receipt,
            expected_predecessor_authority="CONTROLLER-AUTH-1",
            knowledge_ledger=empty_ledger,
        )


# ==============================================================================
# 3. HOLD SEMANTICS TESTS
# ==============================================================================

def test_dependency_hold_releases_only_its_own_matching_dependency_gate(tmp_path: Path):
    """A DEPENDENCY_HOLD for lari_ui_v2 releases when the ui_v2 gate is satisfied."""
    store = CommandAdmissionStore(tmp_path)
    cmd_id = "continue-ui-v2-test"
    cmd_dir = tmp_path / "commands" / cmd_id
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}}), encoding="utf-8")
    store.set_state(
        cmd_id,
        AdmissionState.HOLD,
        authority="DEPENDENCY_GATE",
        reason="DEPENDENCY_HOLD: Waiting for lari_ui_v2 preconditions",
    )

    control = tmp_path / "control"
    control_pc = control / "docs" / "project-control"
    control_pc.mkdir(parents=True)
    (control_pc / "STATE.json").write_text(json.dumps({
        "current_status": "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY",
        "phase7_node2_contract": {"delivery_slices": {"R1": "ACCEPTED_PROVEN", "R2": "ACCEPTED_PROVEN"}},
        "phase7_accepted_execution_chain": {"node2_r2": "b" * 40},
        "accepted_gates": [{"gate": "P7N2-DISCOVERY-MARKETPLACE_R2", "status": "CLOSED_PROVEN", "tested_sha": "b" * 40}],
        "parallel_execution_lanes": {"ui_v2": {"objective": "Ready", "status": "READY"}},
    }), encoding="utf-8")

    activated = evaluate_downstream_gates(control, store)
    assert len(activated) == 1
    assert activated[0].command_id == cmd_id
    assert activated[0].state == AdmissionState.ACTIVE.value


@pytest.mark.parametrize("forbidden_type", ["HUMAN", "SECURITY", "CONTROLLER", "FAILURE", "AUTHORITY", "UNKNOWN_REASON"])
def test_human_security_controller_failure_authority_unknown_hold_remains_hold(tmp_path: Path, forbidden_type: str):
    """Non-dependency holds MUST NEVER auto-release upon canonical gate satisfaction."""
    store = CommandAdmissionStore(tmp_path)
    cmd_id = f"continue-{forbidden_type.lower()}"
    cmd_dir = tmp_path / "commands" / cmd_id
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}}), encoding="utf-8")

    reason = f"{forbidden_type}_HOLD: Manual review required" if forbidden_type != "UNKNOWN_REASON" else "UNKNOWN_REASON"
    authority = f"{forbidden_type}_DECISION" if forbidden_type != "UNKNOWN_REASON" else "NONE"
    store.set_state(cmd_id, AdmissionState.HOLD, authority=authority, reason=reason)

    control = tmp_path / "control"
    control_pc = control / "docs" / "project-control"
    control_pc.mkdir(parents=True)
    (control_pc / "STATE.json").write_text(json.dumps({
        "current_status": "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY",
        "phase7_node2_contract": {"delivery_slices": {"R1": "ACCEPTED_PROVEN", "R2": "ACCEPTED_PROVEN"}},
        "phase7_accepted_execution_chain": {"node2_r2": "b" * 40},
        "accepted_gates": [{"gate": "P7N2-DISCOVERY-MARKETPLACE_R2", "status": "CLOSED_PROVEN", "tested_sha": "b" * 40}],
        "parallel_execution_lanes": {"ui_v2": {"objective": "Ready", "status": "READY"}},
    }), encoding="utf-8")

    activated = evaluate_downstream_gates(control, store)
    assert len(activated) == 0
    assert store.get(cmd_id).state == AdmissionState.HOLD.value


def test_superseded_command_never_reactivates(tmp_path: Path):
    """A command marked SUPERSEDED is ignored by evaluate_downstream_gates and cannot be set to ACTIVE."""
    store = CommandAdmissionStore(tmp_path)
    cmd_id = "continue-superseded-cmd"
    cmd_dir = tmp_path / "commands" / cmd_id
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.json").write_text(json.dumps({"project": {"project_id": "lari-ui-v2"}}), encoding="utf-8")
    store.set_state(cmd_id, AdmissionState.SUPERSEDED, authority="SUPERSEDED", reason="Superseded by newer command")

    control = tmp_path / "control"
    control_pc = control / "docs" / "project-control"
    control_pc.mkdir(parents=True)
    (control_pc / "STATE.json").write_text(json.dumps({
        "current_status": "PHASE_7_NODE_2_DISCOVERY_MARKETPLACE_R3_BOUND_READY",
        "phase7_node2_contract": {"delivery_slices": {"R1": "ACCEPTED_PROVEN", "R2": "ACCEPTED_PROVEN"}},
        "phase7_accepted_execution_chain": {"node2_r2": "b" * 40},
        "accepted_gates": [{"gate": "P7N2-DISCOVERY-MARKETPLACE_R2", "status": "CLOSED_PROVEN", "tested_sha": "b" * 40}],
        "parallel_execution_lanes": {"ui_v2": {"objective": "Ready", "status": "READY"}},
    }), encoding="utf-8")

    activated = evaluate_downstream_gates(control, store)
    assert len(activated) == 0
    assert store.get(cmd_id).state == AdmissionState.SUPERSEDED.value

    with pytest.raises(ValueError, match="Superseded command cannot be reactivated"):
        store.set_state(cmd_id, AdmissionState.ACTIVE, authority="TEST", reason="Reactivate attempt")
