"""Focused regression tests for exact SHA provenance and fail-closed validation."""

import pytest
from pathlib import Path
from aos.provenance import (
    ProvenanceError,
    is_valid_full_sha,
    get_authoritative_git_head,
    validate_exact_sha_provenance,
)


def test_is_valid_full_sha():
    assert is_valid_full_sha("e97faa55b8b3f668055c791a89c8f77966450935")
    assert is_valid_full_sha("454c5cf910cac642afd15c281fe4cbaf83a2507f")
    assert not is_valid_full_sha("e97faa5")  # prefix
    assert not is_valid_full_sha("e97faa502472")  # prefix
    assert not is_valid_full_sha("not-a-sha-at-all-so-should-fail-immediately")
    assert not is_valid_full_sha("")
    assert not is_valid_full_sha(None)
    assert not is_valid_full_sha("g" * 40)  # non-hex


def test_validate_exact_sha_provenance_success():
    sha = "e97faa55b8b3f668055c791a89c8f77966450935"
    result = validate_exact_sha_provenance(
        local_git_head=sha,
        build_source_sha=sha,
        candidate_manifest_source_sha=sha,
        runtime_source_sha=sha,
        github_actions_head_sha=sha,
        require_ci_sha=True,
    )
    assert result.valid
    assert result.status == "PROVEN"
    assert len(result.errors) == 0
    assert result.local_git_head == sha
    assert result.github_actions_head_sha == sha


def test_validate_exact_sha_provenance_fails_closed_on_mismatch():
    sha1 = "e97faa55b8b3f668055c791a89c8f77966450935"
    sha2 = "e97faa502472d829910d9f0466be5ee02b8d0e74"
    result = validate_exact_sha_provenance(
        local_git_head=sha1,
        build_source_sha=sha1,
        candidate_manifest_source_sha=sha2,
        runtime_source_sha=sha2,
        github_actions_head_sha=sha1,
    )
    assert not result.valid
    assert result.status == "FAIL"
    assert any("Provenance mismatch" in err for err in result.errors)


def test_validate_exact_sha_provenance_fails_closed_on_prefix():
    sha1 = "e97faa55b8b3f668055c791a89c8f77966450935"
    prefix = "e97faa502472"
    result = validate_exact_sha_provenance(
        local_git_head=sha1,
        build_source_sha=prefix,
        candidate_manifest_source_sha=sha1,
        runtime_source_sha=sha1,
    )
    assert not result.valid
    assert result.status == "FAIL"
    assert any("not a valid 40-character hex SHA" in err for err in result.errors)


def test_validate_exact_sha_provenance_missing_field():
    sha1 = "e97faa55b8b3f668055c791a89c8f77966450935"
    result = validate_exact_sha_provenance(
        local_git_head=sha1,
        build_source_sha=sha1,
        candidate_manifest_source_sha=None,
        runtime_source_sha=sha1,
    )
    assert not result.valid
    assert result.status == "FAIL"
    assert any("missing or empty" in err for err in result.errors)


def test_get_authoritative_git_head(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    sha = get_authoritative_git_head(repo_root)
    assert is_valid_full_sha(sha)
    assert len(sha) == 40


def test_provenance_runtime_path_is_strictly_headless():
    """Verify raw subprocess.run is NOT used in aos.provenance runtime path."""
    import inspect
    import aos.provenance as prov_module
    src = inspect.getsource(prov_module)
    assert "subprocess.run" not in src
    assert "run_headless" in src


def test_control_panel_does_not_equate_sha_format_with_provenance_proof():
    """Verify control panel build_status marks UNPROVEN if full provenance chain is absent, even with 40-char SHA."""
    from aos.control_panel import build_status
    dummy_config = {
        "runtime_root": "C:/dummy_nonexistent_root",
        "default_project": {},
    }
    # Unconfigured / standalone runtime
    status = build_status(dummy_config)
    assert status["provenance_status"] in ("UNPROVEN", "FAIL")
    assert status["provenance_status"] != "PROVEN"
    assert status["production"] == "NO_GO"


def test_validate_exact_sha_provenance_rejects_synthetic_head():
    """Verify that validate_exact_sha_provenance strictly rejects synthetic/invalid SHA."""
    authoritative_head = "001d509725c28697897932495a9fefbf3d3d5122"
    synthetic_head = "001d509bc19cf3994c653065a452140bbd7a31b6"

    result = validate_exact_sha_provenance(
        local_git_head=authoritative_head,
        build_source_sha=authoritative_head,
        candidate_manifest_source_sha=authoritative_head,
        runtime_source_sha=authoritative_head,
        github_actions_head_sha=synthetic_head,
        require_ci_sha=True,
    )
    assert not result.valid
    assert result.status == "FAIL"
    assert any("Provenance mismatch" in err for err in result.errors)


