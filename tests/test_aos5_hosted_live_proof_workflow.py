"""Static contract tests for AOS-5 Hosted Multi-Machine Live Proof GitHub Actions Workflow."""

from __future__ import annotations

from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aos5-hosted-live-proof.yml"


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), f"Workflow file not found at {WORKFLOW_PATH}"


def test_workflow_trigger_contract():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Assert push trigger only
    assert "push:" in content, "Workflow must include push trigger"
    assert "feature/aos-5-distributed-coordination" in content
    assert "docs/project-control/AOS5_LIVE_PROOF_AUTHORIZATION.json" in content

    # Assert disallowed triggers NOT present
    for disallowed in ["workflow_dispatch:", "pull_request:", "pull_request_target:", "schedule:", "repository_dispatch:"]:
        assert disallowed not in content, f"Disallowed trigger present: {disallowed}"


def test_workflow_permissions_and_concurrency():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Permissions contents read
    assert "permissions:" in content
    assert "contents: read" in content

    # Concurrency
    assert "concurrency:" in content
    assert "group: aos5-hosted-live-proof" in content
    assert "cancel-in-progress: false" in content


def test_workflow_jobs_structure():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Required jobs
    assert "preflight:" in content
    assert "worker-live-execution:" in content
    assert "verify-proof:" in content

    # Worker matrix and environment
    assert "environment: aos5-live-proof" in content
    assert "max-parallel: 2" in content
    assert "role: [worker_a, worker_b]" in content or "role:\n" in content and "worker_a" in content and "worker_b" in content


def test_workflow_safety_and_no_secrets_leak():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    # Assert forbidden commands/flags
    assert "printenv" not in content, "printenv must not be used in workflow"
    assert "set -x" not in content, "set -x must not be used in workflow"
    assert "postgresql://" not in content, "No DSN literal allowed in workflow"
    assert "postgres://" not in content, "No DSN literal allowed in workflow"

    # Secret input reference
    assert "${{ secrets.AOS_POSTGRES_LIVE_DSN }}" in content
    assert "AOS_POSTGRES_LIVE_DSN:" in content

    # RUNNER_TEMP usage
    assert "runner.temp" in content or "RUNNER_TEMP" in content

    # Authority and provider parameters
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
