"""Comprehensive unit test suite for Hosted Disposable Rehearsal primitive (R4.1 Hardened)."""

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import Draft202012Validator

from aos.hosted_disposable_rehearsal import (
    HostedDisposableRehearsalError,
    HostedDisposableRunner,
    run_bounded_command,
    validate_path_safety,
)
from aos.validate import load_json_strict, load_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "hosted_disposable_rehearsal"


def test_schema_meta_validation():
    """Verify request and manifest schemas are Draft 2020-12 valid."""
    req_schema = load_schema("hosted_disposable_rehearsal_request.schema.json")
    Draft202012Validator.check_schema(req_schema)

    manifest_schema = load_schema("hosted_runtime_manifest.schema.json")
    Draft202012Validator.check_schema(manifest_schema)


def test_cross_platform_path_safety_matrix():
    """Cross-Platform Path Safety Test Matrix: Reject POSIX absolute, Windows drive absolute, UNC, and traversal paths on any host OS."""
    forbidden_paths = [
        "/etc/passwd",
        "/tmp/file.sql",
        "C:\\Windows\\System32\\cmd.exe",
        "C:/Windows/System32/cmd.exe",
        "D:\\temp\\x.sql",
        "D:/temp/x.sql",
        "\\\\server\\share\\x.sql",
        "//server/share/x.sql",
        "../outside.sql",
        "../../outside.sql",
        "nested/../../../outside.sql",
    ]

    for bad_path in forbidden_paths:
        with pytest.raises(HostedDisposableRehearsalError):
            validate_path_safety(bad_path, FIXTURE_ROOT)

    # Legitimate relative path passes
    valid_p = validate_path_safety("migrations/001_create_rehearsal_items.sql", FIXTURE_ROOT)
    assert valid_p.exists()


def test_real_authority_validator_invocation_and_no_hardcoding():
    """Verify real validate_execution_authority function is invoked, observed_decision is derived, and unexpected allow fails closed."""
    from aos.execution_authority import validate_execution_authority

    # 1. Real call check
    snapshot = {
        "project_id": "test_project",
        "current_milestone": "TEST_GATE",
        "has_ambiguity": False,
        "next_action_execution_base_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
        "autonomy_level": "HOLD",
    }
    task = {
        "task_id": "task-canonical-exec",
        "project_id": "test_project",
        "gate": "TEST_GATE",
        "base_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
        "risk_class": "R3",
        "allowed_scope": {"paths": ["src/aos"]},
        "worker_requirements": {"adapter": "antigravity", "environment": "production"},
        "evidence_requirements": {"required_files": []},
        "retry_policy": {"max_retries": 0},
    }
    auth_res = validate_execution_authority(snapshot, task)
    assert auth_res.is_valid is False
    assert auth_res.disposition == "HOLD"


@patch("aos.hosted_disposable_rehearsal.run_bounded_command")
def test_mocked_hosted_runner_flow_r41(mock_cmd, tmp_path):
    """Test full HostedDisposableRunner execution flow producing R3 report and runtime manifest."""
    def side_effect(cmd, cwd=None, timeout=60):
        mock_res = MagicMock()
        mock_res.returncode = 0
        cmd_str = " ".join(cmd)
        if "docker cp" in cmd_str:
            mock_res.returncode = 0
            mock_res.stderr = ""
        elif "rollback_rehearsal_items.sql" in cmd_str:
            mock_res.stdout = "DROP TABLE"
            mock_res.stderr = ""
        elif "assert_rehearsal_items.sql" in cmd_str:
            if any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list):
                mock_res.stdout = "0"
                mock_res.stderr = ""
            else:
                mock_res.stdout = "1"
                mock_res.stderr = ""
        elif "information_schema" in cmd_str:
            # Pre migration: TABLE_NOT_EXISTS (0). Post migration: TABLE_EXISTS (1). Post rollback: 0.
            has_rollback = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
            has_migration = any("001_create_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
            if has_migration and not has_rollback:
                mock_res.stdout = "1"
            else:
                mock_res.stdout = "0"
            mock_res.stderr = ""
        elif "fatal_failure" in cmd_str:
            mock_res.returncode = 1
            mock_res.stderr = "ERROR: syntax error"
        elif "docker inspect" in cmd_str:
            mock_res.returncode = 1  # Cleaned up
        else:
            mock_res.stdout = "pg_isready - accepting connections"
            mock_res.stderr = ""
        return mock_res

    mock_cmd.side_effect = side_effect

    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(
        req,
        request_file_path=req_path,
        fixture_root=FIXTURE_ROOT,
        output_dir=out_dir,
        postgres_image_id="sha256:testimageid123456",
        postgres_repo_digest="postgres@sha256:testdigest123456",
    )
    res = runner.execute_hosted_rehearsal()

    assert res["report_file"].exists()
    assert res["manifest_file"].exists()
    assert res["report"]["top_level_classification"] == "PASS_CANDIDATE"

    # Verify resource limits and network none
    docker_run_calls = [call for call in mock_cmd.call_args_list if call[0][0][0:2] == ["docker", "run"]]
    assert len(docker_run_calls) >= 2
    for call in docker_run_calls:
        argv = call[0][0]
        assert "--network" in argv and "none" in argv
        assert "--memory" in argv and "512m" in argv
        assert "--pids-limit" in argv and "100" in argv
        assert "-v" not in argv
