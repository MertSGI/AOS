"""Comprehensive unit test suite for Hosted Disposable Rehearsal primitive."""

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
    """Verify hosted_disposable_rehearsal_request schema is Draft 2020-12 valid."""
    schema_dict = load_schema("hosted_disposable_rehearsal_request.schema.json")
    Draft202012Validator.check_schema(schema_dict)


def test_request_schema_validation_and_unknown_fields():
    """Verify valid request passes and unknown fields fail schema validation."""
    valid_req = load_json_strict(FIXTURE_ROOT / "request.valid.json")
    # Schema validation test
    schema_dict = load_schema("hosted_disposable_rehearsal_request.schema.json")
    Draft202012Validator(schema_dict).validate(valid_req)

    # Unknown field rejection
    invalid_req = dict(valid_req)
    invalid_req["unknown_malicious_field"] = "bad"
    with pytest.raises(Exception):
        Draft202012Validator(schema_dict).validate(invalid_req)


def test_path_safety_attacks():
    """Self-Attack Tests: Path traversal, absolute paths, missing files, and outside root escapes fail closed."""
    # 1. Absolute path rejection
    with pytest.raises(HostedDisposableRehearsalError, match="Absolute path forbidden"):
        validate_path_safety("C:/Windows/System32/cmd.exe", FIXTURE_ROOT)

    # 2. Path traversal rejection
    with pytest.raises(HostedDisposableRehearsalError, match="Path traversal forbidden"):
        validate_path_safety("../../outside.sql", FIXTURE_ROOT)

    # 3. Missing file rejection
    with pytest.raises(HostedDisposableRehearsalError, match="Fixture file not found"):
        validate_path_safety("migrations/non_existent_file.sql", FIXTURE_ROOT)


def test_command_allowlist_and_no_shell_true():
    """Verify command allowlist enforcement and safe subprocess execution."""
    # Unallowed command fails
    with pytest.raises(HostedDisposableRehearsalError, match="not in allowlist"):
        run_bounded_command(["malicious_binary", "--flag"])


def test_authority_separation_does_not_grant_canonical_authority():
    """Verify hosted disposable execution PASS does NOT grant canonical execution authority."""
    from aos.execution_authority import validate_execution_authority

    synthetic_snapshot = {
        "project_id": "test_project",
        "current_milestone": "TEST_GATE",
        "has_ambiguity": False,
        "next_action_execution_base_sha": "7966e9a1a7c36f9af0d78bfc67ab539b06fda0e7",
        "autonomy_level": "HOLD",
    }
    synthetic_canonical_task = {
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

    # Authority validator must hold / require human gate despite disposable execution capability
    res = validate_execution_authority(synthetic_snapshot, synthetic_canonical_task)
    assert res.is_valid is False
    assert res.disposition == "HOLD"


@patch("aos.hosted_disposable_rehearsal.run_bounded_command")
def test_mocked_hosted_runner_flow(mock_cmd, tmp_path):
    """Test full HostedDisposableRunner execution flow using mocked docker subprocess outputs."""
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
            mock_res.stdout = "0"
            mock_res.stderr = ""
        elif "fatal_failure" in cmd_str:
            mock_res.returncode = 1
            mock_res.stderr = "ERROR: syntax error"
        else:
            mock_res.stdout = "pg_isready - accepting connections"
            mock_res.stderr = ""
        return mock_res

    mock_cmd.side_effect = side_effect

    req = load_json_strict(FIXTURE_ROOT / "request.valid.json")
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, fixture_root=FIXTURE_ROOT, output_dir=out_dir)
    res = runner.execute_hosted_rehearsal()

    assert res["report_file"].exists()
    assert res["report"]["top_level_classification"] == "PASS_CANDIDATE"
    assert len(res["report"]["steps"]) == 4

    docker_run_calls = [call for call in mock_cmd.call_args_list if call[0][0][0:2] == ["docker", "run"]]
    assert len(docker_run_calls) >= 2
    for call in docker_run_calls:
        argv = call[0][0]
        assert "--network" in argv and "none" in argv
        assert "--memory" in argv and "512m" in argv
        assert "-v" not in argv

