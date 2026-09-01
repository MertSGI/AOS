"""Comprehensive test suite for rehearsal evidence provenance validation."""

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

from aos.rehearsal_evidence import validate_rehearsal_report
from aos.validate import load_json_strict

REPO_ROOT = Path(__file__).resolve().parent.parent

# Resolve real AOS core symbol metadata for test assertions
import aos.execution_authority
EXEC_AUTH_MOD = "aos.execution_authority"
EXEC_AUTH_SYM = "validate_execution_authority"
EXEC_AUTH_PATH = "src/aos/execution_authority.py"
EXEC_AUTH_FILE = REPO_ROOT / EXEC_AUTH_PATH
EXEC_AUTH_SHA = hashlib.sha256(EXEC_AUTH_FILE.read_bytes()).hexdigest()


def create_base_report(top_level="PASS_CANDIDATE", steps=None):
    if steps is None:
        steps = [
            {
                "step_id": "step-001",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": False,
                    "invocation_identity": "aos.execution_authority.validate_execution_authority",
                    "result_summary": "ACCEPTED",
                },
                "status": "PASS",
                "expected_decision": "ACCEPT",
                "observed_decision": "ACCEPT",
                "evidence_references": ["auth_trace_001"],
            }
        ]

    return {
        "schema_version": "0.1",
        "rehearsal_id": "rehearsal-test-001",
        "target_repo": "MertSGI/AOS",
        "candidate_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
        "top_level_classification": top_level,
        "steps": steps,
    }


def test_a_real_aos_core_component():
    """A. REAL AOS CORE COMPONENT: Real symbol and matching source SHA passes."""
    report = create_base_report()
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "PASS_CANDIDATE"
    assert len(result.errors) == 0


def test_b_fabricated_core_component():
    """B. FABRICATED CORE COMPONENT: Nonexistent module claimed as AOS_CORE must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "PLANNING",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ImaginaryPlanner",
                    "module": "aos.imaginary_planner",
                    "symbol": "ImaginaryPlanner",
                    "source_path": "src/aos/imaginary_planner.py",
                    "source_sha256": "0" * 64,
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": True,
                },
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    assert result.derived_classification == "FAIL"
    codes = [e.code for e in result.errors]
    assert "CORE_MODULE_UNRESOLVED" in codes


def test_c_nonexistent_symbol():
    """C. NONEXISTENT SYMBOL: Valid module with nonexistent symbol must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "STATIC_INSPECTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "NonexistentFunction",
                    "module": EXEC_AUTH_MOD,
                    "symbol": "non_existent_function_12345",
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": False,
                },
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    assert result.derived_classification == "FAIL"
    codes = [e.code for e in result.errors]
    assert "CORE_SYMBOL_UNRESOLVED" in codes


def test_d_core_source_hash_mismatch():
    """D. CORE SOURCE HASH MISMATCH: Real component with wrong SHA256 must fail."""
    wrong_sha = "a" * 64
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": wrong_sha,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": False,
                },
                "status": "FAIL",
                "expected_decision": "ACCEPT",
                "observed_decision": "ACCEPT",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CORE_SOURCE_HASH_MISMATCH" in codes


def test_e_harness_is_not_core():
    """E. HARNESS IS NOT CORE: Harness validated as REHEARSAL_HARNESS passes, but as AOS_CORE fails."""
    # Valid harness step
    harness_step = {
        "step_id": "step-harness",
        "claim_class": "DISPOSABLE_EXECUTION",
        "required_for_pass_candidate": True,
        "component": {
            "origin": "REHEARSAL_HARNESS",
            "name": "TestHarnessH1",
            "description": "Synthetic local harness",
        },
        "execution_provenance": {
            "executed": True,
            "synthetic": True,
        },
        "status": "PASS",
    }
    report = create_base_report(top_level="PASS_CANDIDATE", steps=[harness_step])
    res1 = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert res1.is_valid is True

    # Falsely claiming harness as AOS_CORE
    invalid_step = dict(harness_step)
    invalid_step["component"] = {
        "origin": "AOS_CORE",
        "name": "TestHarnessH1",
        "module": "aos.harness",
        "symbol": "TestHarnessH1",
        "source_path": "src/aos/harness.py",
        "source_sha256": "f" * 64,
    }
    invalid_step["status"] = "FAIL"
    report_invalid = create_base_report(top_level="FAIL", steps=[invalid_step])
    res2 = validate_rehearsal_report(report_invalid, repo_root=REPO_ROOT)
    assert res2.is_valid is False
    codes = [e.code for e in res2.errors]
    assert "CORE_MODULE_UNRESOLVED" in codes


def test_f_conceptual_runtime_pass():
    """F. CONCEPTUAL RUNTIME PASS: CONCEPTUAL + RUNTIME_EXECUTION + PASS must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "RUNTIME_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "CONCEPTUAL",
                    "name": "FutureEngineComponent",
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CONCEPTUAL_EXECUTION_PASS_FORBIDDEN" in codes


def test_g_execution_pass_without_execution():
    """G. EXECUTION PASS WITHOUT EXECUTION: RUNTIME_EXECUTION + executed=false + status=PASS must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "RUNTIME_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "name": "MockRunner",
                    "description": "Mock harness",
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": True,
                },
                "status": "PASS",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "EXECUTION_PASS_WITHOUT_EXECUTION" in codes


def test_h_static_evidence_inflation():
    """H. STATIC EVIDENCE INFLATION: Static inspection attempting DEPLOYMENT_EXECUTION PASS must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "DEPLOYMENT_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": False,
                },
                "status": "PASS",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "EXECUTION_PASS_WITHOUT_EXECUTION" in codes or "EVIDENCE_CLASS_INFLATION" in codes


def test_i_not_executed_without_blocker():
    """I. NOT_EXECUTED WITHOUT BLOCKER: status=NOT_EXECUTED with empty blocker must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "MIGRATION_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "CONCEPTUAL",
                    "name": "MigrationRunner",
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": False,
                },
                "status": "NOT_EXECUTED",
                "blocker": "",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "NOT_EXECUTED_WITHOUT_BLOCKER" in codes


def test_j_authority_decision_match():
    """J. AUTHORITY DECISION MATCH: expected decision equals observed decision passes."""
    report = create_base_report(
        top_level="PASS_CANDIDATE",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
                "expected_decision": "BLOCK_CANONICAL_EXECUTION",
                "observed_decision": "BLOCK_CANONICAL_EXECUTION",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "PASS_CANDIDATE"


def test_k_authority_decision_mismatch():
    """K. AUTHORITY DECISION MISMATCH: expected decision != observed decision must fail."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-001",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
                "expected_decision": "BLOCK_CANONICAL_EXECUTION",
                "observed_decision": "ACCEPT_CANONICAL_EXECUTION",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "AUTHORITY_DECISION_MISMATCH" in codes


def test_l_required_not_executed_aggregation():
    """L. REQUIRED NOT_EXECUTED AGGREGATION: One required step NOT_EXECUTED derives HOLD_CAPABILITY_NOT_EXECUTED."""
    report = create_base_report(
        top_level="HOLD_CAPABILITY_NOT_EXECUTED",
        steps=[
            {
                "step_id": "step-pass",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
                "expected_decision": "BLOCK_CANONICAL_EXECUTION",
                "observed_decision": "BLOCK_CANONICAL_EXECUTION",
            },
            {
                "step_id": "step-not-exec",
                "claim_class": "MIGRATION_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "CONCEPTUAL",
                    "name": "MigrationExecutor",
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": False,
                },
                "status": "NOT_EXECUTED",
                "blocker": "Ephemeral database container unavailable",
            },
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "HOLD_CAPABILITY_NOT_EXECUTED"


def test_m_all_required_pass():
    """M. ALL REQUIRED PASS: All required steps PASS derives PASS_CANDIDATE."""
    report = create_base_report(
        top_level="PASS_CANDIDATE",
        steps=[
            {
                "step_id": "step-1",
                "claim_class": "AUTHORITY_DECISION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ExecutionAuthorityValidator",
                    "module": EXEC_AUTH_MOD,
                    "symbol": EXEC_AUTH_SYM,
                    "source_path": EXEC_AUTH_PATH,
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
                "expected_decision": "ACCEPT",
                "observed_decision": "ACCEPT",
            },
            {
                "step_id": "step-2",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "name": "DisposableRunner",
                    "description": "Local test harness",
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "PASS",
            },
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "PASS_CANDIDATE"


def test_n_any_required_fail():
    """N. ANY REQUIRED FAIL: Step status FAIL derives FAIL."""
    report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-fail",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "name": "DisposableRunner",
                    "description": "Local test harness",
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                },
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "FAIL"


def test_o_caller_supplied_classification_lie():
    """O. CALLER-SUPPLIED CLASSIFICATION LIE: Claiming PASS_CANDIDATE when derived is HOLD must fail."""
    report = create_base_report(
        top_level="PASS_CANDIDATE",  # Claiming PASS when 1 step is NOT_EXECUTED
        steps=[
            {
                "step_id": "step-not-exec",
                "claim_class": "MIGRATION_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "CONCEPTUAL",
                    "name": "MigrationExecutor",
                },
                "execution_provenance": {
                    "executed": False,
                    "synthetic": False,
                },
                "status": "NOT_EXECUTED",
                "blocker": "Database missing",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "TOP_LEVEL_CLASSIFICATION_MISMATCH" in codes


def test_p_unknown_fields():
    """P. UNKNOWN FIELDS: Unknown unsupported fields rejected by JSON schema strictness."""
    report = create_base_report()
    report["unknown_field_123"] = "invalid"
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "SCHEMA_INVALID" in codes


def test_q_duplicate_json_key_support(tmp_path):
    """Q. DUPLICATE JSON KEY SUPPORT: Strict JSON loader rejects duplicate keys."""
    json_file = tmp_path / "dup_key_report.json"
    content = '{"schema_version": "0.1", "schema_version": "0.1"}'
    json_file.write_text(content)

    with pytest.raises(Exception):
        load_json_strict(json_file)
