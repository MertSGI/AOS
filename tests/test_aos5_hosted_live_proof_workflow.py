"""Static contract tests for AOS-5 Hosted Multi-Machine Live Proof GitHub Actions Workflow."""

from __future__ import annotations

import importlib
from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos5-hosted-live-proof.yml"


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), f"Workflow file not found at {WORKFLOW_PATH}"


def test_workflow_trigger_contract():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "push:" in content, "Workflow must include push trigger"
    assert "feature/aos-5-distributed-coordination" in content
    assert "docs/project-control/AOS5_LIVE_PROOF_AUTHORIZATION.json" in content

    for disallowed in ["workflow_dispatch:", "pull_request:", "pull_request_target:", "schedule:", "repository_dispatch:"]:
        assert disallowed not in content, f"Disallowed trigger present: {disallowed}"


def test_workflow_permissions_and_concurrency():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "permissions:" in content
    assert "contents: read" in content

    assert "concurrency:" in content
    assert "group: aos5-hosted-live-proof" in content
    assert "cancel-in-progress: false" in content


def test_workflow_jobs_structure():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "preflight:" in content
    assert "worker-live-execution:" in content
    assert "verify-proof:" in content

    assert "environment: aos5-live-proof" in content
    assert "max-parallel: 2" in content
    assert "role: [worker_a, worker_b]" in content or ("role:" in content and "worker_a" in content and "worker_b" in content)


def test_workflow_safety_and_no_secrets_leak():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "printenv" not in content, "printenv must not be used in workflow"
    assert "set -x" not in content, "set -x must not be used in workflow"
    assert "postgresql://" not in content, "No DSN literal allowed in workflow"
    assert "postgres://" not in content, "No DSN literal allowed in workflow"

    assert "${{ secrets.AOS_POSTGRES_LIVE_DSN }}" in content
    assert "AOS_POSTGRES_LIVE_DSN:" in content

    assert "runner.temp" in content or "RUNNER_TEMP" in content

    assert "weathered-flower-55573540" in content
    assert "NEON_POSTGRES" in content
    assert "cc43b5d13d886bf4beb27c2d41361063efd63fe0" in content
    assert "docs/project-control/AOS5_LIVE_PROOF_AUTHORIZATION.json" in content
    assert "lari_access_allowed" in content
    assert "production_mutation_allowed" in content
    assert "destructive_operations_allowed" in content
    assert "billing_activation_allowed" in content


def test_workflow_pair_verifier_contract():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "verify_pair_results" in content
    assert "5.0" in content
    assert "pair-verification.json" in content
    assert "proof_scoped_machine_fingerprint_sha256" in content


# =====================================================================
# STAGE 11D-B1-R1 SPECIFIC CONTRACT TESTS
# =====================================================================

def test_workflow_r1_packaging_installation_contract():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Assert PREFLIGHT installs canonical editable package with postgres extra
    assert 'python -m pip install -e ".[postgres]"' in content

    # Assert WORKER installs canonical editable package with postgres extra
    assert "WORKER_DOES_NOT_INSTALL_PSYCOPG2_BINARY" not in content
    assert "psycopg2-binary" not in content, "psycopg2-binary ad-hoc install must be removed"

    # Assert all jobs install editable package before AOS imports
    install_count = content.count('python -m pip install -e ".[postgres]"')
    assert install_count == 3, f"Expected 3 occurrences of editable postgres install (preflight, worker, verify), found {install_count}"


def test_workflow_r1_exact_authorization_keyset_enforcement():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Assert exact expected key set defined
    assert "expected_keys = {" in content or "expected_keys = set(" in content
    assert "actual_keys != expected_keys" in content or "actual_keys = set(auth_data.keys())" in content

    # Assert strict duplicate JSON loader present
    assert "load_json_strict" in content

    # Assert recursive secret key rejection present
    assert "check_forbidden_secret_keys(auth_data)" in content or "check_forbidden_secret_keys" in content

    # Check key names present in expected keyset
    expected_fields = [
        "schema_version",
        "artifact_type",
        "gate",
        "authorized",
        "authorization_id",
        "provider",
        "provider_project_id",
        "environment_class",
        "production_mutation_allowed",
        "destructive_operations_allowed",
        "billing_activation_allowed",
        "secret_publication_allowed",
        "lari_access_allowed",
        "approved_harness_carrier_sha",
    ]
    for field in expected_fields:
        assert f"'{field}'" in content or f'"{field}"' in content, f"Expected field '{field}' missing from workflow"


def test_safe_local_executability_imports():
    """Optional safe local executability test to verify local AOS package resolution."""
    val_mod = importlib.import_module("aos.validate")
    assert hasattr(val_mod, "load_json_strict")

    proof_mod = importlib.import_module("aos.coordination_live_proof")
    assert hasattr(proof_mod, "validate_proof_request_dict")
    assert hasattr(proof_mod, "run_live_worker")
    assert hasattr(proof_mod, "verify_pair_results")
