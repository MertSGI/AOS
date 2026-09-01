"""Comprehensive test suite for rehearsal evidence provenance validation (R2 Hardened)."""

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aos.rehearsal_evidence import validate_rehearsal_report
from aos.validate import SCHEMA_DIR, TYPE_TO_SCHEMA, load_json_strict, load_schema

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


def test_schema_meta_validation():
    """Verify rehearsal_report.schema.json is valid Draft 2020-12 and registered in TYPE_TO_SCHEMA."""
    assert "rehearsal_report" in TYPE_TO_SCHEMA
    schema_dict = load_schema(TYPE_TO_SCHEMA["rehearsal_report"])
    Draft202012Validator.check_schema(schema_dict)


def test_a_real_aos_core_component():
    """A. REAL AOS CORE COMPONENT: Real symbol and matching source SHA passes."""
    report = create_base_report()
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is True
    assert result.derived_classification == "PASS_CANDIDATE"
    assert len(result.errors) == 0


def test_aos_core_reexported_symbol_source_spoof():
    """Defect A Regression: Re-exported symbol validate_document from aos.execution_authority must inspect validate.py, rejecting validate_execution_authority.py path spoofing."""
    import aos.execution_authority
    import aos.validate

    real_val_file = REPO_ROOT / "src/aos/validate.py"
    real_val_sha = hashlib.sha256(real_val_file.read_bytes()).hexdigest()

    # Falsely claiming validate_document's source path is execution_authority.py
    spoofed_report = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-spoof",
                "claim_class": "STATIC_INSPECTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ReexportedValidateDocument",
                    "module": "aos.execution_authority",
                    "symbol": "validate_document",
                    "source_path": "src/aos/execution_authority.py",
                    "source_sha256": EXEC_AUTH_SHA,
                },
                "execution_provenance": {"executed": False, "synthetic": True},
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(spoofed_report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CORE_SOURCE_PATH_MISMATCH" in codes

    # Truthful declaration pointing to src/aos/validate.py passes
    truthful_report = create_base_report(
        top_level="PASS_CANDIDATE",
        steps=[
            {
                "step_id": "step-truthful",
                "claim_class": "STATIC_INSPECTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "AOS_CORE",
                    "name": "ReexportedValidateDocument",
                    "module": "aos.execution_authority",
                    "symbol": "validate_document",
                    "source_path": "src/aos/validate.py",
                    "source_sha256": real_val_sha,
                },
                "execution_provenance": {"executed": False, "synthetic": True},
                "status": "PASS",
            }
        ],
    )
    res_truth = validate_rehearsal_report(truthful_report, repo_root=REPO_ROOT)
    assert res_truth.is_valid is True


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
                "execution_provenance": {"executed": False, "synthetic": True},
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
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
                "execution_provenance": {"executed": False, "synthetic": False},
                "status": "FAIL",
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CORE_SYMBOL_UNRESOLVED" in codes


def test_d_core_source_hash_mismatch():
    """D. CORE SOURCE HASH MISMATCH: Real component with wrong SHA256 must fail."""
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
                    "source_sha256": "a" * 64,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": False,
                    "invocation_identity": "aos.execution_authority.validate_execution_authority",
                    "result_summary": "ACCEPT",
                },
                "status": "FAIL",
                "expected_decision": "ACCEPT",
                "observed_decision": "ACCEPT",
                "evidence_references": ["ref1"],
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CORE_SOURCE_HASH_MISMATCH" in codes


def test_harness_file_backed_and_in_memory_modes(tmp_path):
    """Defect B Regressions: FILE_BACKED and IN_MEMORY harness identity modes."""
    harness_dir = tmp_path / "harness"
    harness_dir.mkdir()
    harness_file = harness_dir / "my_harness.py"
    harness_file.write_text("print('harness')")
    actual_harness_sha = hashlib.sha256(harness_file.read_bytes()).hexdigest()

    # 1. FILE_BACKED PASS
    fb_report = create_base_report(
        top_level="PASS_CANDIDATE",
        steps=[
            {
                "step_id": "step-fb",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "FILE_BACKED",
                    "name": "MyHarness",
                    "source_path": "my_harness.py",
                    "source_sha256": actual_harness_sha,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "python my_harness.py",
                    "result_summary": "OK",
                },
                "status": "PASS",
                "evidence_references": ["ref_harness_1"],
            }
        ],
    )
    res_fb = validate_rehearsal_report(fb_report, repo_root=REPO_ROOT, harness_root=harness_dir)
    assert res_fb.is_valid is True

    # 2. HARNESS_FILE_MISSING
    fb_report_missing = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-missing",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "FILE_BACKED",
                    "name": "MyHarness",
                    "source_path": "non_existent_harness.py",
                    "source_sha256": actual_harness_sha,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "run",
                    "result_summary": "OK",
                },
                "status": "FAIL",
                "evidence_references": ["ref"],
            }
        ],
    )
    res_missing = validate_rehearsal_report(fb_report_missing, repo_root=REPO_ROOT, harness_root=harness_dir)
    assert res_missing.is_valid is False
    assert "HARNESS_FILE_MISSING" in [e.code for e in res_missing.errors]

    # 3. HARNESS_FILE_HASH_MISMATCH
    fb_report_wrong_hash = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-bad-hash",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "FILE_BACKED",
                    "name": "MyHarness",
                    "source_path": "my_harness.py",
                    "source_sha256": "b" * 64,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "run",
                    "result_summary": "OK",
                },
                "status": "FAIL",
                "evidence_references": ["ref"],
            }
        ],
    )
    res_bad_hash = validate_rehearsal_report(fb_report_wrong_hash, repo_root=REPO_ROOT, harness_root=harness_dir)
    assert res_bad_hash.is_valid is False
    assert "HARNESS_FILE_HASH_MISMATCH" in [e.code for e in res_bad_hash.errors]

    # 4. HARNESS_PATH_TRAVERSAL
    fb_report_traversal = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-traversal",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "FILE_BACKED",
                    "name": "MyHarness",
                    "source_path": "../outside.py",
                    "source_sha256": actual_harness_sha,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "run",
                    "result_summary": "OK",
                },
                "status": "FAIL",
                "evidence_references": ["ref"],
            }
        ],
    )
    res_traversal = validate_rehearsal_report(fb_report_traversal, repo_root=REPO_ROOT, harness_root=harness_dir)
    assert res_traversal.is_valid is False
    assert "HARNESS_PATH_TRAVERSAL" in [e.code for e in res_traversal.errors]

    # 5. IN_MEMORY HARNESS PASS
    desc_str = "Synthetic in-memory harness description"
    desc_sha = hashlib.sha256(desc_str.encode("utf-8")).hexdigest()
    im_report = create_base_report(
        top_level="PASS_CANDIDATE",
        steps=[
            {
                "step_id": "step-im",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "IN_MEMORY",
                    "name": "InMemoryHarness",
                    "description": desc_str,
                    "identity_sha256": desc_sha,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "in_memory_callable",
                    "result_summary": "OK",
                },
                "status": "PASS",
                "evidence_references": ["ref_im_1"],
            }
        ],
    )
    res_im = validate_rehearsal_report(im_report, repo_root=REPO_ROOT)
    assert res_im.is_valid is True

    # 6. IN_MEMORY_HARNESS_HASH_MISMATCH
    im_report_bad_hash = create_base_report(
        top_level="FAIL",
        steps=[
            {
                "step_id": "step-im-bad",
                "claim_class": "DISPOSABLE_EXECUTION",
                "required_for_pass_candidate": True,
                "component": {
                    "origin": "REHEARSAL_HARNESS",
                    "identity_mode": "IN_MEMORY",
                    "name": "InMemoryHarness",
                    "description": desc_str,
                    "identity_sha256": "c" * 64,
                },
                "execution_provenance": {
                    "executed": True,
                    "synthetic": True,
                    "invocation_identity": "in_memory_callable",
                    "result_summary": "OK",
                },
                "status": "FAIL",
                "evidence_references": ["ref_im_1"],
            }
        ],
    )
    res_im_bad = validate_rehearsal_report(im_report_bad_hash, repo_root=REPO_ROOT)
    assert res_im_bad.is_valid is False
    assert "IN_MEMORY_HARNESS_HASH_MISMATCH" in [e.code for e in res_im_bad.errors]


def test_execution_pass_evidence_requirements():
    """Defect C Regressions: Execution claim classes PASS requires executed=true, invocation_identity, result_summary, evidence_references."""
    base_step = {
        "step_id": "step-exec",
        "claim_class": "RUNTIME_EXECUTION",
        "required_for_pass_candidate": True,
        "component": {
            "origin": "REHEARSAL_HARNESS",
            "identity_mode": "IN_MEMORY",
            "name": "Runner",
            "description": "desc",
            "identity_sha256": hashlib.sha256(b"desc").hexdigest(),
        },
        "execution_provenance": {
            "executed": True,
            "synthetic": True,
            "invocation_identity": "my_runner",
            "result_summary": "SUCCESS",
        },
        "status": "PASS",
        "evidence_references": ["ref_1"],
    }

    # 1. Truthful execution PASS
    report_truthful = create_base_report(top_level="PASS_CANDIDATE", steps=[base_step])
    assert validate_rehearsal_report(report_truthful, repo_root=REPO_ROOT).is_valid is True

    # 2. Missing invocation_identity
    s2 = json.loads(json.dumps(base_step))
    s2["execution_provenance"]["invocation_identity"] = ""
    r2 = create_base_report(top_level="FAIL", steps=[s2])
    res2 = validate_rehearsal_report(r2, repo_root=REPO_ROOT)
    assert res2.is_valid is False
    assert "EXECUTION_PASS_WITHOUT_INVOCATION_IDENTITY" in [e.code for e in res2.errors]

    # 3. Missing result_summary
    s3 = json.loads(json.dumps(base_step))
    s3["execution_provenance"]["result_summary"] = ""
    r3 = create_base_report(top_level="FAIL", steps=[s3])
    res3 = validate_rehearsal_report(r3, repo_root=REPO_ROOT)
    assert res3.is_valid is False
    assert "EXECUTION_PASS_WITHOUT_RESULT_IDENTITY" in [e.code for e in res3.errors]

    # 4. Missing evidence_references
    s4 = json.loads(json.dumps(base_step))
    s4["evidence_references"] = []
    r4 = create_base_report(top_level="FAIL", steps=[s4])
    res4 = validate_rehearsal_report(r4, repo_root=REPO_ROOT)
    assert res4.is_valid is False
    assert "EXECUTION_PASS_WITHOUT_EVIDENCE_REFERENCE" in [e.code for e in res4.errors]


def test_authority_decision_provenance_requirements():
    """Defect D Regressions: AUTHORITY_DECISION PASS requires AOS_CORE origin, executed=true, evidence references, matching decisions."""
    auth_step = {
        "step_id": "step-auth",
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
            "invocation_identity": "aos.execution_authority.validate_execution_authority",
            "result_summary": "HOLD",
        },
        "status": "PASS",
        "expected_decision": "BLOCK_CANONICAL_EXECUTION",
        "observed_decision": "BLOCK_CANONICAL_EXECUTION",
        "evidence_references": ["auth_trace_999"],
    }

    # 1. Real AOS Authority PASS accepted
    r1 = create_base_report(top_level="PASS_CANDIDATE", steps=[auth_step])
    assert validate_rehearsal_report(r1, repo_root=REPO_ROOT).is_valid is True

    # 2. AUTHORITY_PASS_WITHOUT_EXECUTION_REJECTED
    s2 = json.loads(json.dumps(auth_step))
    s2["execution_provenance"]["executed"] = False
    r2 = create_base_report(top_level="FAIL", steps=[s2])
    res2 = validate_rehearsal_report(r2, repo_root=REPO_ROOT)
    assert res2.is_valid is False
    assert "AUTHORITY_PASS_WITHOUT_EXECUTION_REJECTED" in [e.code for e in res2.errors]

    # 3. AUTHORITY_PASS_HARNESS_ORIGIN_REJECTED
    s3 = json.loads(json.dumps(auth_step))
    s3["component"] = {
        "origin": "REHEARSAL_HARNESS",
        "identity_mode": "IN_MEMORY",
        "name": "MockAuthHarness",
        "description": "Mock",
        "identity_sha256": hashlib.sha256(b"Mock").hexdigest(),
    }
    r3 = create_base_report(top_level="FAIL", steps=[s3])
    res3 = validate_rehearsal_report(r3, repo_root=REPO_ROOT)
    assert res3.is_valid is False
    assert "AUTHORITY_PASS_HARNESS_ORIGIN_REJECTED" in [e.code for e in res3.errors]

    # 4. AUTHORITY_PASS_WITHOUT_EVIDENCE_REJECTED
    s4 = json.loads(json.dumps(auth_step))
    s4["evidence_references"] = []
    r4 = create_base_report(top_level="FAIL", steps=[s4])
    res4 = validate_rehearsal_report(r4, repo_root=REPO_ROOT)
    assert res4.is_valid is False
    assert "AUTHORITY_PASS_WITHOUT_EVIDENCE_REJECTED" in [e.code for e in res4.errors]

    # 5. AUTHORITY_DECISION_MISMATCH_REJECTED
    s5 = json.loads(json.dumps(auth_step))
    s5["observed_decision"] = "ALLOW_EXECUTION"
    r5 = create_base_report(top_level="FAIL", steps=[s5])
    res5 = validate_rehearsal_report(r5, repo_root=REPO_ROOT)
    assert res5.is_valid is False
    assert "AUTHORITY_DECISION_MISMATCH" in [e.code for e in res5.errors]


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
                    "invocation_identity": "future_call",
                    "result_summary": "OK",
                },
                "status": "PASS",
                "evidence_references": ["ref"],
            }
        ],
    )
    result = validate_rehearsal_report(report, repo_root=REPO_ROOT)
    assert result.is_valid is False
    codes = [e.code for e in result.errors]
    assert "CONCEPTUAL_EXECUTION_PASS_FORBIDDEN" in codes


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
                    "invocation_identity": "aos.execution_authority.validate_execution_authority",
                    "result_summary": "HOLD",
                },
                "status": "PASS",
                "expected_decision": "BLOCK_CANONICAL_EXECUTION",
                "observed_decision": "BLOCK_CANONICAL_EXECUTION",
                "evidence_references": ["auth_ref"],
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
