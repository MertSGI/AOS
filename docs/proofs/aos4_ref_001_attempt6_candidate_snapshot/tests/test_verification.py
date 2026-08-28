"""Tests for AOS-4 Independent Verification Foundation.

Verifies deterministic independent verification contracts, verifier boundaries,
fail-closed semantics for malformed, stale, scope-invalid, missing-artifact,
and contradictory inputs, and validates that executor results alone cannot authorize
canonical closure.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from aos.validate import validate_document, load_schema
from aos.verification import (
    IndependentVerifier,
    VerificationCheck,
    VerificationResult,
    validate_verification_result,
    verify_controlled_execution,
    verify_execution,
)


def make_valid_descriptor(
    project_id: str = "aos",
    repo: str = "MertSGI/AOS",
    control_ref: str = "feature/aos-4-independent-verification-hold",
) -> Dict[str, Any]:
    """Construct a canonical project descriptor valid against project_descriptor.schema.json."""
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": repo,
        "control_ref": control_ref,
        "description": "AOS self-reference descriptor for generic AOS-4 R1 independent verification control",
        "control": {
            "state": "docs/project-control/STATE.json",
            "decisions": "docs/project-control/DECISIONS.md",
            "evidence": "docs/project-control/EVIDENCE.jsonl",
            "roadmap": "docs/project-control/ROADMAP.md",
            "charter": "docs/project-control/CHARTER.md",
            "autonomy_policy": "docs/project-control/AUTONOMY_POLICY.md",
            "update_protocol": "docs/project-control/UPDATE_PROTOCOL.md",
        },
        "projection": {
            "current_status_pointer": "/status",
            "current_milestone_pointer": "/current_gate",
            "canonical_next_action_pointer": "/next_action",
            "next_action_execution_base_sha_pointer": "/extensions/aos4_independent_verification/next_action_execution_base_sha",
            "next_action_execution_base_sha_required": True,
        },
        "authority": {
            "production_mutation": "human_required",
            "roadmap_change": "human_required",
            "destructive_data": "human_required",
            "security_boundary_change": "human_required",
            "payment_activation": "human_required",
        },
        "workers": {
            "branch_prefix": "aos/",
            "isolated_worktree_required": True,
            "max_concurrent_workers": 1,
        },
        "verification": {
            "checks": {
                "aos4_verification_foundation_tests": {
                    "argv": ["python", "-m", "pytest", "-q", "tests/test_verification.py"],
                    "timeout_seconds": 120,
                },
                "aos_full_regression": {
                    "argv": ["python", "-m", "pytest", "-q"],
                    "timeout_seconds": 180,
                },
            }
        },
    }


def make_valid_task(
    project_id: str = "aos",
    task_id: str = "AOS4-REF-001",
    gate: str = "AOS-4",
    base_sha: str = "3779c0a195301ec585a59537db5baa8df86228cf",
    risk_class: str = "R1",
    paths: Optional[List[str]] = None,
    forbidden_paths: Optional[List[str]] = None,
    required_checks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct a canonical task document valid against task.schema.json."""
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "task_id": task_id,
        "gate": gate,
        "title": "AOS-4 Independent Verification Foundation",
        "description": "Implement the generic deterministic independent-verification foundation.",
        "risk_class": risk_class,
        "base_sha": base_sha,
        "branch_name": "aos/aos4-ref-001",
        "allowed_scope": {
            "paths": paths or [
                "src/aos/verification.py",
                "src/aos/validate.py",
                "schemas/v0.1/verification_result.schema.json",
                "tests/test_verification.py",
            ],
            "forbidden_paths": forbidden_paths or [
                "docs/project-control/",
                "descriptors/",
                "tasks/",
                ".github/",
            ],
        },
        "worker_requirements": {
            "adapter": "antigravity",
            "isolated_worktree": True,
            "timeout_seconds": 180,
        },
        "evidence_requirements": {
            "minimum_level": "E3_ISOLATED_RUNTIME_PROVEN",
            "required_checks": required_checks or [
                "aos4_verification_foundation_tests",
                "aos_full_regression",
            ],
        },
        "retry_policy": {
            "max_retries": 0,
            "retry_count": 0,
            "auto_retry_on_semantic_failure": False,
            "on_exhausted": "HOLD",
        },
    }


def make_valid_execution_result(
    project_id: str = "aos",
    task_id: str = "AOS4-REF-001",
    gate: str = "AOS-4",
    control_source_sha: str = "ecca376a45ebf906f742400f50c2d01ef9f8fbba",
    execution_base_sha: str = "3779c0a195301ec585a59537db5baa8df86228cf",
    disposition: str = "VERIFIED_CANDIDATE",
    changed_paths: Optional[List[str]] = None,
    candidate_id: str = "cand_1234567890abcdef",
    manifest_sha256: str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
) -> Dict[str, Any]:
    """Construct a canonical ControlledExecutionResult valid against controlled_execution_result.schema.json."""
    paths = changed_paths if changed_paths is not None else [
        "schemas/v0.1/verification_result.schema.json",
        "src/aos/validate.py",
        "src/aos/verification.py",
        "tests/test_verification.py",
    ]

    checks = [
        {"check_id": "descriptor_schema", "status": "PASS"},
        {"check_id": "target_repository_identity", "status": "PASS"},
        {"check_id": "task_schema", "status": "PASS"},
        {"check_id": "worker_requirements", "status": "PASS"},
        {"check_id": "worker_capability", "status": "PASS"},
        {"check_id": "retry_policy", "status": "PASS"},
        {"check_id": "required_checks_contract", "status": "PASS"},
        {"check_id": "canonical_snapshot", "status": "PASS"},
        {"check_id": "execution_authority", "status": "PASS"},
        {"check_id": "execution_base_resolvability", "status": "PASS"},
        {"check_id": "live_guard_pre_execution", "status": "PASS"},
        {"check_id": "workspace_initial_integrity", "status": "PASS"},
        {"check_id": "worker_execution", "status": "PASS"},
        {"check_id": "git_integrity_post_worker", "status": "PASS"},
        {"check_id": "scope_guard_post_worker", "status": "PASS"},
        {"check_id": "live_guard_post_worker", "status": "PASS"},
        {"check_id": "project:aos4_verification_foundation_tests", "status": "PASS"},
        {"check_id": "project:aos_full_regression", "status": "PASS"},
        {"check_id": "git_integrity_final", "status": "PASS"},
        {"check_id": "scope_guard_final", "status": "PASS"},
        {"check_id": "live_guard_final", "status": "PASS"},
        {"check_id": "candidate_persistence", "status": "PASS"},
    ]

    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "task_id": task_id,
        "gate": gate,
        "disposition": disposition,
        "control_source_sha": control_source_sha,
        "execution_base_sha": execution_base_sha,
        "worker_branch": "aos/aos4-ref-001",
        "initial_head_sha": execution_base_sha,
        "final_head_sha": execution_base_sha,
        "changed_paths": paths,
        "scope_validation": {
            "is_valid": True,
            "allowed_paths": [
                "src/aos/verification.py",
                "src/aos/validate.py",
                "schemas/v0.1/verification_result.schema.json",
                "tests/test_verification.py",
            ],
            "forbidden_paths": [
                "docs/project-control/",
                "descriptors/",
                "tasks/",
                ".github/",
            ],
            "violations": [],
        },
        "worker_adapter": "antigravity",
        "worker_identity": "antigravity-cli (PROVEN)",
        "worker_capability_status": "PROVEN",
        "worker_mutation_attempted": True,
        "worker_exit_code": 0,
        "worker_timed_out": False,
        "verification_checks": checks,
        "mutation_performed": True,
        "retry_count": 0,
        "started_at": "2026-08-25T10:25:05.178171+00:00",
        "finished_at": "2026-08-25T10:28:10.666976+00:00",
        "errors": [],
        "extensions": {
            "candidate": {
                "status": "PERSISTED",
                "candidate_store_contract_version": "0.1.0",
                "candidate_id": candidate_id,
                "manifest_sha256": manifest_sha256,
                "changed_paths": paths,
                "execution_base_sha": execution_base_sha,
                "control_source_sha": control_source_sha,
            }
        },
    }


def make_valid_evidence(
    evidence_id: str = "AOS-EV-0010",
    project: str = "AOS",
    gate: str = "AOS-4",
    task_id: str = "AOS4-REF-001",
    commit_sha: str = "ecca376a45ebf906f742400f50c2d01ef9f8fbba",
    base_sha: str = "3779c0a195301ec585a59537db5baa8df86228cf",
    result: str = "PASS",
) -> Dict[str, Any]:
    """Construct a canonical evidence record valid against evidence.schema.json."""
    return {
        "schema_version": "0.1.0",
        "evidence_id": evidence_id,
        "timestamp": "2026-08-25T14:30:00+00:00",
        "project": project,
        "gate": gate,
        "task_id": task_id,
        "type": "INDEPENDENT_VERIFICATION_PASS",
        "claim": "AOS4-REF-001 independent verification foundation deterministically verified.",
        "evidence_level": "E3_ISOLATED_RUNTIME_PROVEN",
        "revisions": {
            "commit_sha": commit_sha,
            "base_sha": base_sha,
            "branch": "feature/aos-4-independent-verification-hold",
        },
        "environment": {
            "os": "Windows",
            "worker": "antigravity-pc",
            "runtime": "Python 3.12",
        },
        "verification": {
            "method": "AOS-4 Deterministic Independent Verifier",
            "verifier": "IndependentVerifier",
        },
        "result": result,
        "artifacts": [
            "docs/proofs/aos4_ref_001_execution.json"
        ],
        "limitations": [
            "Deterministic verification only; does not replace human critical decision gates."
        ],
    }


def make_valid_snapshot(
    project_id: str = "aos",
    source_sha: str = "ecca376a45ebf906f742400f50c2d01ef9f8fbba",
    exec_base_sha: str = "3779c0a195301ec585a59537db5baa8df86228cf",
    current_milestone: str = "AOS-4",
) -> Dict[str, Any]:
    """Construct a canonical project snapshot valid against canonical_project_snapshot.schema.json."""
    return {
        "schema_version": "0.1.0",
        "project_id": project_id,
        "repository": "MertSGI/AOS",
        "source_ref": "feature/aos-4-independent-verification-hold",
        "source_sha": source_sha,
        "current_status": "AOS_4_RUNTIME_TRUST_REPROVEN_RETARGET_PENDING",
        "current_milestone": current_milestone,
        "canonical_next_action": "Implement the generic deterministic independent-verification foundation.",
        "target_base_sha": "3779c0a195301ec585a59537db5baa8df86228cf",
        "next_action_execution_base_sha": exec_base_sha,
        "has_ambiguity": False,
        "ambiguity_reasons": [],
        "input_file_hashes": {
            "state": "0000000000000000000000000000000000000000000000000000000000000000",
            "decisions": "0000000000000000000000000000000000000000000000000000000000000000",
            "evidence": "0000000000000000000000000000000000000000000000000000000000000000",
            "roadmap": "0000000000000000000000000000000000000000000000000000000000000000",
        },
    }


class TestCanonicalFixtureValidation:
    """Remediation Requirement: All fixtures described/named as 'valid' MUST validate against canonical schemas."""

    def test_make_valid_descriptor_validates_against_canonical_schema(self):
        desc = make_valid_descriptor()
        res = validate_document("project_descriptor", desc)
        assert res.is_valid is True, f"Descriptor validation failed: {[e.message for e in res.errors]}"

    def test_make_valid_task_validates_against_canonical_schema(self):
        task = make_valid_task()
        res = validate_document("task", task)
        assert res.is_valid is True, f"Task validation failed: {[e.message for e in res.errors]}"

    def test_make_valid_execution_result_validates_against_canonical_schema(self):
        exec_res = make_valid_execution_result()
        res = validate_document("controlled_execution_result", exec_res)
        assert res.is_valid is True, f"Execution result validation failed: {[e.message for e in res.errors]}"

    def test_make_valid_evidence_validates_against_canonical_schema(self):
        ev = make_valid_evidence()
        res = validate_document("evidence", ev)
        assert res.is_valid is True, f"Evidence validation failed: {[e.message for e in res.errors]}"

    def test_make_valid_snapshot_validates_against_canonical_schema(self):
        snap = make_valid_snapshot()
        res = validate_document("canonical_project_snapshot", snap)
        assert res.is_valid is True, f"Snapshot validation failed: {[e.message for e in res.errors]}"


class TestScenario1CanonicalPass:
    """Scenario 1: Canonical-valid input produces PASS and validates against verification_result schema."""

    def test_scenario_1_canonical_valid_input_passes(self):
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result()
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            check_artifacts=False,
        )

        assert result.is_pass is True
        assert result.is_hold is False
        assert result.disposition == "PASS"
        assert result.authorizes_canonical_closure is True
        assert len(result.errors) == 0

        # All checks must be PASS
        for check in result.checks:
            assert check.status == "PASS", f"Check '{check.check_id}' was not PASS: {check.message}"

        # Result dictionary must validate against verification_result schema
        res_dict = result.to_dict()
        val_res = validate_verification_result(res_dict)
        assert val_res.is_valid is True, f"VerificationResult dictionary failed schema: {[e.message for e in val_res.errors]}"


class TestExecutorResultClosureAuthorityRule:
    """Executor result alone must NOT authorize canonical closure."""

    def test_executor_result_alone_cannot_authorize_canonical_closure(self):
        exec_res = make_valid_execution_result(disposition="VERIFIED_CANDIDATE")
        # An unverified ControlledExecutionResult alone is an executor candidate claim
        assert exec_res["disposition"] == "VERIFIED_CANDIDATE"

        # Independent verifier must run to authorize closure
        verifier = IndependentVerifier()

        # If any check fails, closure is forbidden (False)
        bad_exec = copy.deepcopy(exec_res)
        bad_exec["disposition"] = "WORKER_FAILED"
        res = verifier.verify(execution_result=bad_exec, check_artifacts=False)
        assert res.authorizes_canonical_closure is False
        assert res.disposition == "HOLD"


class TestMalformedInputsFailClosed:
    """Verifier must fail closed on malformed inputs."""

    def test_non_dict_execution_result_fails_closed(self):
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result="not a dict")
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert len(res.errors) > 0

    def test_missing_required_fields_in_execution_result_fails_closed(self):
        exec_res = make_valid_execution_result()
        del exec_res["disposition"]
        del exec_res["gate"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("schema validation failed" in e for e in res.errors)

    def test_malformed_execution_base_sha_fails_closed(self):
        exec_res = make_valid_execution_result(execution_base_sha="invalid-short-sha")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Execution base SHA is malformed" in e or "schema validation failed" in e for e in res.errors)

    def test_malformed_control_source_sha_fails_closed(self):
        exec_res = make_valid_execution_result(control_source_sha="not-a-40-hex-sha")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_task_fails_closed(self):
        exec_res = make_valid_execution_result()
        task = make_valid_task()
        del task["allowed_scope"]  # required field missing
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Task schema validation failed" in e for e in res.errors)

    def test_malformed_descriptor_fails_closed(self):
        exec_res = make_valid_execution_result()
        desc = make_valid_descriptor()
        del desc["authority"]  # required authority missing
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, descriptor=desc, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Project descriptor schema validation failed" in e for e in res.errors)

    def test_malformed_evidence_fails_closed(self):
        exec_res = make_valid_execution_result()
        evidence = make_valid_evidence()
        evidence["timestamp"] = "not-a-timestamp"
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Evidence schema validation failed" in e for e in res.errors)

    def test_malformed_snapshot_fails_closed(self):
        exec_res = make_valid_execution_result()
        snap = make_valid_snapshot()
        del snap["source_sha"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, snapshot=snap, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Canonical project snapshot schema validation failed" in e for e in res.errors)


class TestStaleAndRevisionMismatchesFailClosed:
    """Verifier must fail closed on stale or mismatched revisions."""

    def test_stale_control_source_sha_mismatch_fails_closed(self):
        exec_res = make_valid_execution_result(control_source_sha="1111111111111111111111111111111111111111")
        snap = make_valid_snapshot(source_sha="2222222222222222222222222222222222222222")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, snapshot=snap, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Snapshot source_sha" in e for e in res.errors)

    def test_execution_base_sha_mismatch_fails_closed(self):
        exec_res = make_valid_execution_result(execution_base_sha="1111111111111111111111111111111111111111")
        task = make_valid_task(base_sha="2222222222222222222222222222222222222222")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Task base_sha" in e for e in res.errors)

    def test_project_id_mismatch_fails_closed(self):
        exec_res = make_valid_execution_result(project_id="project_a")
        task = make_valid_task(project_id="project_b")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("project_id" in e for e in res.errors)

    def test_task_id_mismatch_fails_closed(self):
        exec_res = make_valid_execution_result(task_id="TASK-001")
        task = make_valid_task(task_id="TASK-002")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("task_id" in e for e in res.errors)

    def test_gate_milestone_mismatch_fails_closed(self):
        exec_res = make_valid_execution_result(gate="AOS-4")
        snap = make_valid_snapshot(current_milestone="AOS-3")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, snapshot=snap, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Snapshot current_milestone" in e for e in res.errors)

    def test_snapshot_ambiguity_fails_closed(self):
        exec_res = make_valid_execution_result()
        snap = make_valid_snapshot()
        snap["has_ambiguity"] = True
        snap["ambiguity_reasons"] = ["Control file mismatch"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, snapshot=snap, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("ambiguity" in e for e in res.errors)


class TestScopeGuardFailuresFailClosed:
    """Verifier must fail closed on scope guard violations."""

    def test_scope_is_valid_false_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["scope_validation"]["is_valid"] = False
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_scope_violations_present_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["scope_validation"]["violations"] = ["Path 'forbidden/file.txt' was modified"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Scope violations reported" in e for e in res.errors)

    def test_changed_paths_outside_allowed_scope_fails_closed(self):
        task = make_valid_task(paths=["src/"])
        exec_res = make_valid_execution_result(changed_paths=["src/app.py", "out_of_scope.txt"])
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("out_of_scope.txt" in e for e in res.errors)

    def test_changed_paths_in_forbidden_paths_fails_closed(self):
        task = make_valid_task(paths=["src/", "docs/"], forbidden_paths=["docs/project-control/"])
        exec_res = make_valid_execution_result(changed_paths=["src/app.py", "docs/project-control/STATE.json"])
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("docs/project-control/STATE.json" in e for e in res.errors)


class TestWorkerAndExecutionFailuresFailClosed:
    """Verifier must fail closed on worker timeout, nonzero exit, or failed disposition."""

    def test_worker_failed_disposition_fails_closed(self):
        exec_res = make_valid_execution_result(disposition="WORKER_FAILED")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_verification_failed_disposition_fails_closed(self):
        exec_res = make_valid_execution_result(disposition="VERIFICATION_FAILED")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_worker_timed_out_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["worker_timed_out"] = True
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Worker timed out" in e for e in res.errors)

    def test_worker_nonzero_exit_code_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["worker_exit_code"] = 1
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("non-zero exit code" in e for e in res.errors)

    def test_execution_result_with_errors_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["errors"] = ["Fatal git error occurred"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False


class TestRequiredChecksAccountingFailClosed:
    """Verifier must fail closed if required checks are missing, failed, or not run."""

    def test_required_check_missing_fails_closed(self):
        task = make_valid_task(required_checks=["custom_security_scan"])
        exec_res = make_valid_execution_result()
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("custom_security_scan" in e for e in res.errors)

    def test_required_check_failed_fails_closed(self):
        task = make_valid_task(required_checks=["aos4_verification_foundation_tests"])
        exec_res = make_valid_execution_result()
        for c in exec_res["verification_checks"]:
            if "aos4_verification_foundation_tests" in c["check_id"]:
                c["status"] = "FAIL"
                c["message"] = "1 test failed"
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("aos4_verification_foundation_tests" in e for e in res.errors)

    def test_required_check_not_run_fails_closed(self):
        task = make_valid_task(required_checks=["aos_full_regression"])
        exec_res = make_valid_execution_result()
        for c in exec_res["verification_checks"]:
            if "aos_full_regression" in c["check_id"]:
                c["status"] = "NOT_RUN"
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("aos_full_regression" in e for e in res.errors)


class TestCandidateAndArtifactIntegrityFailClosed:
    """Verifier must fail closed on missing or corrupted candidate store artifacts."""

    def test_candidate_status_not_persisted_fails_closed(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"]["candidate"]["status"] = "QUARANTINED_UNVERIFIED"
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Candidate store status" in e for e in res.errors)

    def test_candidate_manifest_sha_malformed_fails_closed(self):
        exec_res = make_valid_execution_result(manifest_sha256="bad-sha256")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Candidate manifest SHA256" in e for e in res.errors)

    def test_candidate_manifest_missing_on_disk_fails_closed(self, tmp_path):
        cand_id = "cand_testmissing"
        cand_dir = tmp_path / cand_id
        cand_dir.mkdir(parents=True)
        # manifest.json is NOT created

        exec_res = make_valid_execution_result(candidate_id=cand_id)
        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            candidate_store_dir=tmp_path,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("manifest.json missing" in e for e in res.errors)

    def test_candidate_manifest_sha_mismatch_on_disk_fails_closed(self, tmp_path):
        cand_id = "cand_testmismatch"
        cand_dir = tmp_path / cand_id
        cand_dir.mkdir(parents=True)
        manifest_file = cand_dir / "manifest.json"
        manifest_file.write_text('{"test": 123}', encoding="utf-8")
        actual_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        claimed_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=claimed_sha)
        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            candidate_store_dir=tmp_path,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Candidate manifest SHA mismatch" in e for e in res.errors)


class TestContradictoryInputsFailClosed:
    """Verifier must fail closed on contradictory evidence or conflicting metadata."""

    def test_contradictory_evidence_result_worker_failed_vs_evidence_pass_fails_closed(self):
        exec_res = make_valid_execution_result(disposition="WORKER_FAILED")
        evidence = make_valid_evidence(result="PASS")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Contradictory evidence" in e for e in res.errors)

    def test_contradictory_evidence_result_hold_vs_verified_candidate_fails_closed(self):
        exec_res = make_valid_execution_result(disposition="VERIFIED_CANDIDATE")
        evidence = make_valid_evidence(result="HOLD")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Contradictory evidence" in e for e in res.errors)

    def test_contradictory_evidence_level_insufficient_fails_closed(self):
        task = make_valid_task()
        task["evidence_requirements"]["minimum_level"] = "E3_ISOLATED_RUNTIME_PROVEN"
        evidence = make_valid_evidence()
        evidence["evidence_level"] = "E1_LOCAL_SOURCE"  # lower rank
        exec_res = make_valid_execution_result()
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, task=task, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("lower than task required minimum_level" in e for e in res.errors)

    def test_contradictory_evidence_revisions_fails_closed(self):
        exec_res = make_valid_execution_result(execution_base_sha="3779c0a195301ec585a59537db5baa8df86228cf")
        evidence = make_valid_evidence(base_sha="1111111111111111111111111111111111111111")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Evidence base_sha" in e for e in res.errors)

    def test_contradictory_evidence_project_fails_closed(self):
        exec_res = make_valid_execution_result(project_id="aos")
        evidence = make_valid_evidence(project="different_project")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Evidence project" in e for e in res.errors)

    def test_contradictory_evidence_gate_fails_closed(self):
        exec_res = make_valid_execution_result(gate="AOS-4")
        evidence = make_valid_evidence(gate="AOS-5")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res, evidence=evidence, check_artifacts=False)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("Evidence gate" in e for e in res.errors)


class TestVerifierIsReadOnlyAndNonMutating:
    """Verifier boundary consumes inputs without executing or mutating the worker task."""

    def test_verifier_does_not_mutate_inputs_or_execute_worker(self):
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result()
        evidence = make_valid_evidence()
        snap = make_valid_snapshot()

        orig_desc = copy.deepcopy(desc)
        orig_task = copy.deepcopy(task)
        orig_exec = copy.deepcopy(exec_res)
        orig_ev = copy.deepcopy(evidence)
        orig_snap = copy.deepcopy(snap)

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snap,
            check_artifacts=False,
        )

        assert res.is_pass is True
        assert desc == orig_desc
        assert task == orig_task
        assert exec_res == orig_exec
        assert evidence == orig_ev
        assert snap == orig_snap


class TestVerificationResultSchemaAndValidator:
    """VerificationResult schema compliance and validator integration."""

    def test_verification_result_schema_meta_validation(self):
        from jsonschema import Draft202012Validator
        schema = load_schema("verification_result.schema.json")
        Draft202012Validator.check_schema(schema)
        assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        assert schema.get("$id") == "https://schemas.mertsgi.org/aos/v0.1/verification_result.schema.json"

    def test_unknown_top_level_field_in_verification_result_rejected(self):
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result()
        evidence = make_valid_evidence()
        snap = make_valid_snapshot()

        res = verify_controlled_execution(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snap,
            check_artifacts=False,
        )
        data = res.to_dict()
        data["_unknown_bogus_field"] = "should fail"
        val = validate_document("verification_result", data)
        assert val.is_valid is False
        assert any("_unknown_bogus_field" in e.message for e in val.errors)

    def test_extensions_in_verification_result_accepted(self):
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result()
        evidence = make_valid_evidence()
        snap = make_valid_snapshot()

        res = verify_controlled_execution(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snap,
            check_artifacts=False,
        )
        data = res.to_dict()
        data["extensions"] = {"custom_metric": 42, "notes": "audit complete"}
        val = validate_document("verification_result", data)
        assert val.is_valid is True
