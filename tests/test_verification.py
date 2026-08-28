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
    artifacts: Optional[List[str]] = None,
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
        "artifacts": artifacts if artifacts is not None else [
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


def create_real_temp_candidate_and_artifacts(
    tmp_path: Path,
    candidate_id: str = "cand_1234567890abcdef",
    declared_artifacts: Optional[List[str]] = None,
    changed_paths: Optional[List[str]] = None,
    project_id: str = "aos",
    task_id: str = "AOS4-REF-001",
    gate: str = "AOS-4",
    control_source_sha: str = "ecca376a45ebf906f742400f50c2d01ef9f8fbba",
    execution_base_sha: str = "3779c0a195301ec585a59537db5baa8df86228cf",
    custom_manifest_data: Optional[Dict[str, Any]] = None,
    corrupt_file_sha: bool = False,
    corrupt_file_size: bool = False,
    deleted_file_exists: bool = False,
) -> tuple[Path, Path, str]:
    """Helper to create genuine physical temp candidate store and artifact root matching full contract (Section 14)."""
    cand_store_dir = tmp_path / "candidate_store"
    cand_dir = cand_store_dir / candidate_id
    workspace_dir = cand_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    paths_list = changed_paths if changed_paths is not None else [
        "schemas/v0.1/verification_result.schema.json",
        "src/aos/validate.py",
        "src/aos/verification.py",
        "tests/test_verification.py",
    ]

    manifest_changed_records = []
    for rel_p in paths_list:
        if deleted_file_exists and rel_p == "tests/test_verification.py":
            p = workspace_dir / rel_p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"deleted": false}', encoding="utf-8")
            manifest_changed_records.append({
                "path": rel_p,
                "state": "DELETED",
            })
            continue

        p = workspace_dir / rel_p
        p.parent.mkdir(parents=True, exist_ok=True)
        content = f"// content for {rel_p}\n".encode("utf-8")
        p.write_bytes(content)

        act_size = len(content) if not corrupt_file_size else len(content) + 10
        act_sha = hashlib.sha256(content).hexdigest() if not corrupt_file_sha else "0" * 64

        manifest_changed_records.append({
            "path": rel_p,
            "state": "PRESENT",
            "size_bytes": act_size,
            "sha256": act_sha,
        })

    manifest_dict = custom_manifest_data if custom_manifest_data is not None else {
        "schema_version": "0.1.0",
        "candidate_store_contract_version": "0.1.0",
        "candidate_id": candidate_id,
        "project_id": project_id,
        "task_id": task_id,
        "gate": gate,
        "control_source_sha": control_source_sha,
        "execution_base_sha": execution_base_sha,
        "worker_branch": "aos/aos4-ref-001",
        "initial_head_sha": execution_base_sha,
        "final_head_sha": execution_base_sha,
        "changed_paths": manifest_changed_records,
        "created_at": "2026-08-28T10:00:00Z",
    }

    manifest_file = cand_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_dict, indent=2), encoding="utf-8")
    actual_manifest_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()

    artifact_root = tmp_path / "artifact_root"
    artifact_root.mkdir(parents=True, exist_ok=True)

    artifacts_to_create = declared_artifacts if declared_artifacts is not None else ["docs/proofs/aos4_ref_001_execution.json"]
    for art_path in artifacts_to_create:
        p = artifact_root / art_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"proof": true}', encoding="utf-8")

    return cand_store_dir, artifact_root, actual_manifest_sha


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


class TestAOS4RequiredSixScenarioMatrix:
    """Canonical AOS-4 Required Scenario Matrix Tests (Section 15)."""

    def test_aos4_required_scenario_1_valid_pass(self, tmp_path):
        cand_id = "cand_0000000000000001"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)

        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_pass is True
        assert result.is_hold is False
        assert result.disposition == "PASS"
        assert result.authorizes_canonical_closure is True
        assert len(result.errors) == 0

        for check in result.checks:
            assert check.status == "PASS", f"Check '{check.check_id}' was not PASS: {check.message}"

        res_dict = result.to_dict()
        val_res = validate_verification_result(res_dict)
        assert val_res.is_valid is True, f"VerificationResult dictionary failed schema: {[e.message for e in val_res.errors]}"

    def test_aos4_required_scenario_2_failing_deterministic_test_holds(self, tmp_path):
        cand_id = "cand_0000000000000002"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)

        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        for c in exec_res["verification_checks"]:
            if "aos4_verification_foundation_tests" in c["check_id"]:
                c["status"] = "FAIL"
                c["message"] = "Deterministic unit test failure"
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_hold is True
        assert result.disposition == "HOLD"
        assert result.authorizes_canonical_closure is False

    def test_aos4_required_scenario_3_browser_claim_missing_artifact_holds(self, tmp_path):
        cand_id = "cand_0000000000000003"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, declared_artifacts=["browser/proof.json"]
        )
        proof_file = artifact_root / "browser" / "proof.json"
        if proof_file.exists():
            proof_file.unlink()

        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(artifacts=["browser/proof.json"])
        evidence["type"] = "BROWSER_RUNTIME_PROOF"
        evidence["claim"] = "Executor claims browser runtime proof."
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_hold is True
        assert result.disposition == "HOLD"
        assert result.authorizes_canonical_closure is False
        assert any("evidence_artifact_integrity" in c.check_id and c.status == "FAIL" for c in result.checks)

    def test_aos4_required_scenario_4_stale_sha_holds(self, tmp_path):
        cand_id = "cand_0000000000000004"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)

        desc = make_valid_descriptor()
        task = make_valid_task(base_sha="1111111111111111111111111111111111111111")
        exec_res = make_valid_execution_result(execution_base_sha="2222222222222222222222222222222222222222", candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_hold is True
        assert result.disposition == "HOLD"
        assert result.authorizes_canonical_closure is False

    def test_aos4_required_scenario_5_scope_violation_holds(self, tmp_path):
        cand_id = "cand_0000000000000005"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)

        desc = make_valid_descriptor()
        task = make_valid_task(paths=["src/"], forbidden_paths=["docs/project-control/"])
        exec_res = make_valid_execution_result(changed_paths=["docs/project-control/STATE.json"], candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_hold is True
        assert result.disposition == "HOLD"
        assert result.authorizes_canonical_closure is False
        assert any("scope_guard_verification" in c.check_id and c.status == "FAIL" for c in result.checks)

    def test_aos4_required_scenario_6_contradictory_accepted_evidence_holds(self, tmp_path):
        cand_id = "cand_0000000000000006"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)

        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(disposition="VERIFIED_CANDIDATE", candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(result="HOLD")
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        result = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert result.is_hold is True
        assert result.disposition == "HOLD"
        assert result.authorizes_canonical_closure is False
        assert any("evidence_consistency" in c.check_id and c.status == "FAIL" for c in result.checks)


class TestMalformedNestedInputRegressionMatrix:
    """Section 11: Malformed nested inputs MUST return HOLD and MUST NOT raise exceptions."""

    def test_malformed_extensions_list(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_scope_validation_list(self):
        exec_res = make_valid_execution_result()
        exec_res["scope_validation"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_task_allowed_scope_list(self):
        task = make_valid_task()
        task["allowed_scope"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=make_valid_execution_result(), task=task)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_task_evidence_requirements_list(self):
        task = make_valid_task()
        task["evidence_requirements"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=make_valid_execution_result(), task=task)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_descriptor_verification_list(self):
        desc = make_valid_descriptor()
        desc["verification"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=make_valid_execution_result(), descriptor=desc)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_evidence_revisions_list(self):
        ev = make_valid_evidence()
        ev["revisions"] = []
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=make_valid_execution_result(), evidence=ev)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_malformed_snapshot_ambiguity_reasons_string(self):
        snap = make_valid_snapshot()
        snap["ambiguity_reasons"] = "not-an-array"
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=make_valid_execution_result(), snapshot=snap)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False


class TestMissingAndSpoofedCandidateRegressionMatrix:
    """Section 12: Missing or spoofed candidate store & manifest regressions."""

    def test_a_missing_extensions_holds(self):
        exec_res = make_valid_execution_result()
        del exec_res["extensions"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_b_extensions_empty_dict_holds(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"] = {}
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_c_candidate_extension_missing_holds(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"] = {"other": 123}
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_d_candidate_id_escape_holds(self):
        exec_res = make_valid_execution_result(candidate_id="../escape")
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_e_candidate_extension_base_sha_differs_holds(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"]["candidate"]["execution_base_sha"] = "0" * 40
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_f_candidate_extension_control_sha_differs_holds(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"]["candidate"]["control_source_sha"] = "0" * 40
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_g_candidate_extension_changed_paths_differs_holds(self):
        exec_res = make_valid_execution_result()
        exec_res["extensions"]["candidate"]["changed_paths"] = ["different.py"]
        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_h_physical_manifest_candidate_id_differs_holds(self, tmp_path):
        cand_id = "cand_0000000000000008"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, custom_manifest_data={
                "schema_version": "0.1.0",
                "candidate_store_contract_version": "0.1.0",
                "candidate_id": "cand_0000000000000009",
                "project_id": "aos",
                "task_id": "AOS4-REF-001",
                "gate": "AOS-4",
                "control_source_sha": "ecca376a45ebf906f742400f50c2d01ef9f8fbba",
                "execution_base_sha": "3779c0a195301ec585a59537db5baa8df86228cf",
                "worker_branch": "aos/aos4-ref-001",
                "initial_head_sha": "3779c0a195301ec585a59537db5baa8df86228cf",
                "final_head_sha": "3779c0a195301ec585a59537db5baa8df86228cf",
                "changed_paths": [],
            }
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_i_physical_manifest_project_task_gate_differs_holds(self, tmp_path):
        cand_id = "cand_0000000000000009"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, project_id="different_project"
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_j_physical_manifest_shas_differs_holds(self, tmp_path):
        cand_id = "cand_0000000000000010"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, execution_base_sha="0" * 40
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_k_physical_manifest_changed_paths_differs_holds(self, tmp_path):
        cand_id = "cand_0000000000000011"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, changed_paths=["src/aos/verification.py"]
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_l_manifest_present_file_sha_mismatch_holds(self, tmp_path):
        cand_id = "cand_0000000000000012"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, corrupt_file_sha=True
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_m_manifest_present_file_size_mismatch_holds(self, tmp_path):
        cand_id = "cand_0000000000000013"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, corrupt_file_size=True
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False

    def test_n_manifest_deleted_file_exists_holds(self, tmp_path):
        cand_id = "cand_0000000000000014"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(
            tmp_path, candidate_id=cand_id, deleted_file_exists=True
        )
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False


class TestUnknownEvidenceResultRegression:
    """Section 13: Non-PASS evidence result (e.g. UNKNOWN) MUST hold."""

    def test_unknown_evidence_result_holds(self, tmp_path):
        cand_id = "cand_0000000000000015"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(result="UNKNOWN")
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("evidence_consistency" in c.check_id and c.status == "FAIL" for c in res.checks)


class TestExecutorResultAloneRegression:
    """Authoritative regression for Blocker #1."""

    def test_executor_result_alone_cannot_authorize_canonical_closure(self):
        exec_res = make_valid_execution_result(disposition="VERIFIED_CANDIDATE")
        assert exec_res["disposition"] == "VERIFIED_CANDIDATE"

        verifier = IndependentVerifier()
        res = verifier.verify(execution_result=exec_res)

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("independent_context_complete" in c.check_id and c.status == "FAIL" for c in res.checks)


class TestArtifactBypassRegressions:
    """Authoritative regressions for artifact bypasses."""

    def test_regression_a_check_artifacts_false_holds(self, tmp_path):
        cand_id = "cand_0000000000000016"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=False,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("artifact_verification_policy" in c.check_id and c.status == "FAIL" for c in res.checks)

    def test_regression_b_missing_candidate_store_dir_holds(self, tmp_path):
        cand_id = "cand_0000000000000017"
        _, artifact_root, _ = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id)
        evidence = make_valid_evidence()
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=None,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("candidate_and_artifact_integrity" in c.check_id and c.status == "FAIL" for c in res.checks)

    def test_regression_c_missing_artifact_root_when_evidence_declares_artifacts_holds(self, tmp_path):
        cand_id = "cand_0000000000000018"
        cand_store_dir, _, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(artifacts=["docs/proofs/aos4_ref_001_execution.json"])
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=None,
            check_artifacts=True,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("evidence_artifact_integrity" in c.check_id and c.status == "FAIL" for c in res.checks)

    def test_regression_d_missing_browser_artifact_holds(self, tmp_path):
        cand_id = "cand_0000000000000019"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id, declared_artifacts=[])
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(artifacts=["browser/proof.json"])
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("evidence_artifact_integrity" in c.check_id and c.status == "FAIL" for c in res.checks)

    def test_regression_e_artifact_path_traversal_holds(self, tmp_path):
        cand_id = "cand_0000000000000020"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id, declared_artifacts=[])
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence(artifacts=["../outside-proof.json"])
        snapshot = make_valid_snapshot()

        verifier = IndependentVerifier()
        res = verifier.verify(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snapshot,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )

        assert res.disposition == "HOLD"
        assert res.authorizes_canonical_closure is False
        assert any("evidence_artifact_integrity" in c.check_id and c.status == "FAIL" for c in res.checks)


class TestVerifierIsReadOnlyAndNonMutating:
    """Verifier boundary consumes inputs without executing or mutating the worker task."""

    def test_verifier_does_not_mutate_inputs_or_execute_worker(self, tmp_path):
        cand_id = "cand_0000000000000021"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
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
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
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

    def test_unknown_top_level_field_in_verification_result_rejected(self, tmp_path):
        cand_id = "cand_0000000000000022"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snap = make_valid_snapshot()

        res = verify_controlled_execution(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snap,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        data = res.to_dict()
        data["_unknown_bogus_field"] = "should fail"
        val = validate_document("verification_result", data)
        assert val.is_valid is False
        assert any("_unknown_bogus_field" in e.message for e in val.errors)

    def test_extensions_in_verification_result_accepted(self, tmp_path):
        cand_id = "cand_0000000000000023"
        cand_store_dir, artifact_root, actual_manifest_sha = create_real_temp_candidate_and_artifacts(tmp_path, candidate_id=cand_id)
        desc = make_valid_descriptor()
        task = make_valid_task()
        exec_res = make_valid_execution_result(candidate_id=cand_id, manifest_sha256=actual_manifest_sha)
        evidence = make_valid_evidence()
        snap = make_valid_snapshot()

        res = verify_controlled_execution(
            execution_result=exec_res,
            task=task,
            descriptor=desc,
            evidence=evidence,
            snapshot=snap,
            candidate_store_dir=cand_store_dir,
            artifact_root=artifact_root,
            check_artifacts=True,
        )
        data = res.to_dict()
        data["extensions"] = {"custom_metric": 42, "notes": "audit complete"}
        val = validate_document("verification_result", data)
        assert val.is_valid is True
