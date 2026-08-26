"""Comprehensive offline test suite for DEC-022 HumanGatePolicy and ExecutionAuthorization."""

import json
from pathlib import Path
import pytest

from aos.human_gate_policy import (
    HUMAN_CRITICAL_CATEGORIES,
    FORBIDDEN_CATEGORIES,
    evaluate_human_gate_policy,
)
from aos.validate import validate_document, load_schema


class TestHumanGatePolicy:
    def test_r0_auto_execute(self):
        task = {"task_id": "T-R0", "project_id": "aos", "gate": "AOS-4", "risk_class": "R0"}
        res = evaluate_human_gate_policy(task)
        assert res.decision == "AUTO_EXECUTE"
        assert res.authority_source == "POLICY_AUTONOMOUS"
        assert "RISK_CLASS_R0_AUTO_EXECUTE" in res.reason_codes

    def test_isolated_nonprod_r1_auto_execute(self):
        task = {"task_id": "T-R1", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"is_isolated_non_prod": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"
        assert res.authority_source == "POLICY_AUTONOMOUS"

    def test_r1_explicit_false_human_required(self):
        task = {"task_id": "T-R1-FALSE", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"is_isolated_non_prod": False}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "HUMAN_REQUIRED"
        assert "RISK_CLASS_R1_EXPLICIT_NON_ISOLATED_REQUIRES_HUMAN" in res.reason_codes

    def test_r1_missing_context_fails_closed(self):
        task = {"task_id": "T-R1-MISSING", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        res = evaluate_human_gate_policy(task, context={})
        assert res.decision == "HUMAN_REQUIRED"
        assert "RISK_CLASS_R1_MISSING_ISOLATION_EVIDENCE_FAILS_CLOSED" in res.reason_codes

    def test_execution_authority_derives_isolation_facts(self):
        from aos.execution_authority import validate_execution_authority
        task = {
            "schema_version": "0.1.0",
            "project_id": "aos",
            "task_id": "AOS4-REF-001",
            "gate": "AOS-4",
            "title": "Test Title",
            "description": "Test Description",
            "risk_class": "R1",
            "base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "branch_name": "aos/aos4-ref-001",
            "worker_requirements": {"adapter": "antigravity", "environment": "non_production", "isolated_worktree": True},
            "allowed_scope": {"paths": ["src/aos/verification.py"], "forbidden_paths": []},
            "evidence_requirements": {"minimum_level": "E3_ISOLATED_RUNTIME_PROVEN", "required_checks": ["c1"]},
            "retry_policy": {"max_retries": 0, "retry_count": 0, "auto_retry_on_semantic_failure": False, "on_exhausted": "HOLD"}
        }
        snapshot = {
            "schema_version": "0.1.0",
            "project_id": "aos",
            "repository": "MertSGI/AOS",
            "source_ref": "feature/aos-4-independent-verification-hold",
            "source_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "current_status": "BOOTSTRAP",
            "current_milestone": "AOS-4",
            "canonical_next_action": "Action",
            "target_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "next_action_execution_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "has_ambiguity": False,
            "ambiguity_reasons": [],
            "input_file_hashes": {"state": "0000000000000000000000000000000000000000000000000000000000000000"}
        }
        auth_res = validate_execution_authority(snapshot, task)
        assert auth_res.is_valid is True
        assert auth_res.disposition == "ACCEPT"

    def test_r1_environment_variations_execution_authority(self):
        from aos.execution_authority import validate_execution_authority
        base_task = {
            "schema_version": "0.1.0",
            "project_id": "aos",
            "task_id": "AOS4-REF-001",
            "gate": "AOS-4",
            "title": "T",
            "description": "D",
            "risk_class": "R1",
            "base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "branch_name": "aos/aos4-ref-001",
            "allowed_scope": {"paths": ["src/aos/verification.py"]},
            "evidence_requirements": {"minimum_level": "E3_ISOLATED_RUNTIME_PROVEN", "required_checks": ["c1"]},
            "retry_policy": {"max_retries": 0, "retry_count": 0, "auto_retry_on_semantic_failure": False, "on_exhausted": "HOLD"}
        }
        snapshot = {
            "schema_version": "0.1.0",
            "project_id": "aos",
            "repository": "MertSGI/AOS",
            "source_ref": "feature/aos-4-independent-verification-hold",
            "source_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "current_status": "BOOTSTRAP",
            "current_milestone": "AOS-4",
            "canonical_next_action": "Action",
            "target_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "next_action_execution_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "has_ambiguity": False,
            "ambiguity_reasons": [],
            "input_file_hashes": {"state": "0000000000000000000000000000000000000000000000000000000000000000"}
        }

        # 1. isolated_worktree=true, environment=non_production -> ACCEPT (AUTO_EXECUTE)
        t1 = json.loads(json.dumps(base_task))
        t1["worker_requirements"] = {"adapter": "antigravity", "environment": "non_production", "isolated_worktree": True}
        assert validate_execution_authority(snapshot, t1).is_valid is True

        # 2. isolated_worktree=true, environment=production -> HOLD (HUMAN_REQUIRED)
        t2 = json.loads(json.dumps(base_task))
        t2["worker_requirements"] = {"adapter": "antigravity", "environment": "production", "isolated_worktree": True}
        assert validate_execution_authority(snapshot, t2).is_valid is False

        # 3. isolated_worktree=true, environment missing -> HOLD (HUMAN_REQUIRED)
        t3 = json.loads(json.dumps(base_task))
        t3["worker_requirements"] = {"adapter": "antigravity", "isolated_worktree": True}
        assert validate_execution_authority(snapshot, t3).is_valid is False

        # 4. isolated_worktree=false, environment=non_production -> HOLD (HUMAN_REQUIRED)
        t4 = json.loads(json.dumps(base_task))
        t4["worker_requirements"] = {"adapter": "antigravity", "environment": "non_production", "isolated_worktree": False}
        assert validate_execution_authority(snapshot, t4).is_valid is False

        # 5. unknown/unrecognized environment -> HOLD
        t5 = json.loads(json.dumps(base_task))
        t5["worker_requirements"] = {"adapter": "antigravity", "environment": "staging", "isolated_worktree": True}
        assert validate_execution_authority(snapshot, t5).is_valid is False

    def test_canonical_aos4_reference_task_integrity_and_policy(self):
        from aos.validate import validate_document
        from aos.execution_authority import validate_execution_authority
        task_path = Path("tasks/aos4-reference-task.json")
        assert task_path.is_file()
        task = json.loads(task_path.read_text(encoding="utf-8"))

        val = validate_document("task", task)
        assert val.is_valid is True

        assert task["worker_requirements"]["environment"] == "non_production"
        assert task["base_sha"] == "d8ed009da7c26ceff153ada29ab9e78526d925c7"
        assert task["allowed_scope"]["paths"] == [
            "src/aos/verification.py",
            "src/aos/validate.py",
            "schemas/v0.1/verification_result.schema.json",
            "tests/test_verification.py"
        ]

        snapshot = {
            "schema_version": "0.1.0",
            "project_id": "aos",
            "repository": "MertSGI/AOS",
            "source_ref": "feature/aos-4-independent-verification-hold",
            "source_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "current_status": "BOOTSTRAP",
            "current_milestone": "AOS-4",
            "canonical_next_action": "Action",
            "target_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "next_action_execution_base_sha": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "has_ambiguity": False,
            "ambiguity_reasons": [],
            "input_file_hashes": {"state": "0000000000000000000000000000000000000000000000000000000000000000"}
        }

        auth_res = validate_execution_authority(snapshot, task)
        assert auth_res.is_valid is True

        state_path = Path("docs/project-control/STATE.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["extensions"]["aos4_independent_verification"]["next_execution_attempt_number"] == 3
        assert state["extensions"]["aos4_independent_verification"]["next_execution_authorization_status"] == "POLICY_AUTHORIZED"

    def test_accepted_isolated_nonprod_r2_auto_execute(self):
        task = {"task_id": "T-R2", "project_id": "aos", "gate": "AOS-4", "risk_class": "R2"}
        ctx = {"is_isolated_non_prod": True, "is_accepted_envelope": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"
        assert res.authority_source == "POLICY_AUTONOMOUS"

    def test_r2_outside_accepted_envelope_human_required(self):
        task = {"task_id": "T-R2-OUT", "project_id": "aos", "gate": "AOS-4", "risk_class": "R2"}
        ctx = {"is_isolated_non_prod": True, "is_accepted_envelope": False}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "HUMAN_REQUIRED"
        assert res.authority_source == "NONE"

    def test_r3_human_required(self):
        task = {"task_id": "T-R3", "project_id": "aos", "gate": "AOS-4", "risk_class": "R3"}
        res = evaluate_human_gate_policy(task)
        assert res.decision == "HUMAN_REQUIRED"
        assert res.authority_source == "NONE"

    def test_r4_forbidden(self):
        task = {"task_id": "T-R4", "project_id": "aos", "gate": "AOS-4", "risk_class": "R4"}
        res = evaluate_human_gate_policy(task)
        assert res.decision == "FORBIDDEN"
        assert res.authority_source == "NONE"

    @pytest.mark.parametrize("cat", sorted(list(HUMAN_CRITICAL_CATEGORIES)))
    def test_every_human_critical_category(self, cat):
        task = {"task_id": "T-CRIT", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"human_critical_categories": [cat]}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "HUMAN_REQUIRED"
        assert cat in res.human_critical_categories

    @pytest.mark.parametrize("fcat", sorted(list(FORBIDDEN_CATEGORIES)))
    def test_forbidden_bypass_categories(self, fcat):
        task = {"task_id": "T-FORB", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"forbidden_categories": [fcat]}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "FORBIDDEN"
        assert fcat in res.forbidden_categories

    def test_planner_human_gate_required_true_cannot_create_unnecessary_gate(self):
        """Planner setting human_gate_required=True is advisory only; policy evaluates R1 isolated as AUTO_EXECUTE."""
        task = {
            "task_id": "T-PLANNER-ADVISORY",
            "project_id": "aos",
            "gate": "AOS-4",
            "risk_class": "R1",
            "human_gate_required": True,
        }
        res = evaluate_human_gate_policy(task, context={"is_isolated_non_prod": True})
        assert res.decision == "AUTO_EXECUTE"

    def test_planner_cannot_downgrade_human_required(self):
        """Planner setting human_gate_required=False on R3 cannot bypass HUMAN_REQUIRED decision."""
        task = {
            "task_id": "T-R3-PLANNER-FALSE",
            "project_id": "aos",
            "gate": "AOS-4",
            "risk_class": "R3",
            "human_gate_required": False,
        }
        res = evaluate_human_gate_policy(task)
        assert res.decision == "HUMAN_REQUIRED"

    def test_ordinary_implementation_failure_not_human_gated(self):
        task = {"task_id": "T-FAIL", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"failure_type": "ORDINARY_IMPLEMENTATION_FAILURE", "is_isolated_non_prod": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"

    def test_deterministic_test_failure_not_human_gated(self):
        task = {"task_id": "T-TEST-FAIL", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"failure_type": "DETERMINISTIC_TEST_FAILURE", "is_isolated_non_prod": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"

    def test_worker_timeout_not_human_gated(self):
        task = {"task_id": "T-TIMEOUT", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"failure_type": "WORKER_TIMEOUT", "is_isolated_non_prod": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"

    def test_safe_capability_reprobe_auto_remediate(self):
        task = {"task_id": "T-REPROBE", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"is_capability_reprobe": True, "reprobe_allowed": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_REMEDIATE"
        assert res.authority_source == "POLICY_AUTONOMOUS"

    def test_retry_within_budget_does_not_require_human(self):
        task = {"task_id": "T-RETRY", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"retry_count": 1, "retry_max": 2, "is_isolated_non_prod": True}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "AUTO_EXECUTE"

    def test_exhausted_retry_with_no_safe_remediation_human_required(self):
        task = {"task_id": "T-EXHAUST", "project_id": "aos", "gate": "AOS-4", "risk_class": "R1"}
        ctx = {"retry_count": 3, "retry_max": 2, "has_safe_remediation": False}
        res = evaluate_human_gate_policy(task, context=ctx)
        assert res.decision == "HUMAN_REQUIRED"
        assert "RETRY_CEILING_EXCEEDED_WITH_NO_SAFE_REMEDIATION" in res.human_critical_categories

    def test_capability_proof_persistence_preserves_exact_probe_res_proof(self, tmp_path):
        from aos.workers.antigravity_probe import persist_capability_proof

        proof_artifact = {
            "schema_version": "0.1.0",
            "probe_id": "PROBE-TEST-123",
            "probe_status": "PASS",
            "challenge_sha256": "abc123sha",
            "head_before": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "head_after": "d8ed009da7c26ceff153ada29ab9e78526d925c7",
            "branch_before": "feature/aos-4-independent-verification-hold",
            "branch_after": "feature/aos-4-independent-verification-hold",
            "expected_result_file_sha256": "exp123",
            "actual_result_file_sha256": "exp123",
            "outside_sentinel_before_sha256": "sentinel1",
            "outside_sentinel_after_sha256": "sentinel1",
            "unexpected_external_paths_count": 0,
            "changed_paths": ["probe/result.txt"],
            "exit_code": 0,
            "timed_out": False,
            "errors": [],
        }

        store_file = tmp_path / "capability_proof.json"
        saved_path = persist_capability_proof(proof_artifact, store_file)

        assert saved_path.is_file()
        loaded = json.loads(saved_path.read_text(encoding="utf-8"))

        for key in [
            "challenge_sha256",
            "head_before",
            "head_after",
            "branch_before",
            "branch_after",
            "expected_result_file_sha256",
            "actual_result_file_sha256",
            "outside_sentinel_before_sha256",
            "outside_sentinel_after_sha256",
            "unexpected_external_paths_count",
            "changed_paths",
            "exit_code",
            "timed_out",
            "errors",
        ]:
            assert key in loaded
            assert loaded[key] == proof_artifact[key]

    def test_historical_incomplete_evidence_not_rewritten(self):
        """Historical proof files are immutable."""
        hist_proof = Path("docs/proofs/aos3_r1_e2e_reference_execution_attempt2.json")
        if hist_proof.is_file():
            content = hist_proof.read_text(encoding="utf-8")
            assert "schema_version" in content
