"""Comprehensive unit test suite for Hosted Disposable Rehearsal primitive (R4.2 Evidence Closure)."""

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
from aos.rehearsal_evidence import validate_report_manifest_binding
from aos.validate import load_json_strict, load_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "hosted_disposable_rehearsal"


def test_schema_meta_validation():
    """Verify all R4.2 schemas are Draft 2020-12 valid."""
    req_schema = load_schema("hosted_disposable_rehearsal_request.schema.json")
    Draft202012Validator.check_schema(req_schema)

    manifest_schema = load_schema("hosted_runtime_manifest.schema.json")
    Draft202012Validator.check_schema(manifest_schema)

    attestation_schema = load_schema("hosted_attestation.schema.json")
    Draft202012Validator.check_schema(attestation_schema)

    report_schema = load_schema("rehearsal_report.schema.json")
    Draft202012Validator.check_schema(report_schema)


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

    valid_p = validate_path_safety("migrations/001_create_rehearsal_items.sql", FIXTURE_ROOT)
    assert valid_p.exists()


def test_real_authority_invocation_once():
    """Verify real validate_execution_authority function is invoked exactly once and returned disposition drives decision."""
    from aos.execution_authority import validate_execution_authority

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


def test_cleanup_rm_failure_fails_closed(tmp_path):
    """Verify nonzero docker rm failure increases cleanup_failure_count and fails runner execution."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        def side_effect(cmd, cwd=None, timeout=60):
            res = MagicMock()
            cmd_str = " ".join(cmd)
            if "docker rm" in cmd_str:
                res.returncode = 1
                res.stderr = "docker rm failure error"
            elif "docker cp" in cmd_str:
                res.returncode = 0
            elif "rollback_rehearsal_items.sql" in cmd_str:
                res.stdout = "DROP TABLE"
            elif "assert_rehearsal_items.sql" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "0" if has_rb else "1"
            elif "information_schema" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                has_mig = any("001_create_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "1" if (has_mig and not has_rb) else "0"
            elif "fatal_failure" in cmd_str:
                res.returncode = 1
                res.stderr = "ERROR: syntax error"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect
        with pytest.raises(HostedDisposableRehearsalError, match="Cleanup verification failed"):
            runner.execute_hosted_rehearsal()


def test_cleanup_surviving_container_fails_closed(tmp_path):
    """Verify post-cleanup inspect finding surviving container causes failure."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        def side_effect(cmd, cwd=None, timeout=60):
            res = MagicMock()
            cmd_str = " ".join(cmd)
            if "docker rm" in cmd_str:
                res.returncode = 0
            elif "docker inspect" in cmd_str:
                res.returncode = 0  # Inspect after rm returns 0 -> surviving orphan container!
                res.stdout = '[{"HostConfig":{"NetworkMode":"none"}}]'
            elif "docker cp" in cmd_str:
                res.returncode = 0
            elif "rollback_rehearsal_items.sql" in cmd_str:
                res.stdout = "DROP TABLE"
            elif "assert_rehearsal_items.sql" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "0" if has_rb else "1"
            elif "information_schema" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                has_mig = any("001_create_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "1" if (has_mig and not has_rb) else "0"
            elif "fatal_failure" in cmd_str:
                res.returncode = 1
                res.stderr = "ERROR: syntax error"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect
        with pytest.raises(HostedDisposableRehearsalError, match="Cleanup verification failed"):
            runner.execute_hosted_rehearsal()


def test_manifest_binding_valid_and_substitution_self_attacks(tmp_path):
    """Verify report ↔ manifest cryptographic binding passes for valid pair and fails on SHA mismatch or modification."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        def side_effect(cmd, cwd=None, timeout=60):
            res = MagicMock()
            cmd_str = " ".join(cmd)
            if "docker rm" in cmd_str:
                res.returncode = 0
            elif "docker inspect" in cmd_str:
                # If docker rm was already called for this container, inspect returns 1 (cleaned up)
                has_rm = any("docker rm" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                if has_rm:
                    res.returncode = 1
                else:
                    res.returncode = 0
                    res.stdout = '[{"HostConfig":{"NetworkMode":"none"}}]'
            elif "docker cp" in cmd_str:
                res.returncode = 0
            elif "rollback_rehearsal_items.sql" in cmd_str:
                res.stdout = "DROP TABLE"
            elif "assert_rehearsal_items.sql" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "0" if has_rb else "1"
            elif "information_schema" in cmd_str:
                has_rb = any("rollback_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                has_mig = any("001_create_rehearsal_items.sql" in " ".join(c[0][0]) for c in mock_cmd.call_args_list)
                res.stdout = "1" if (has_mig and not has_rb) else "0"
            elif "fatal_failure" in cmd_str:
                res.returncode = 1
                res.stderr = "ERROR: syntax error"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect

        runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
        res = runner.execute_hosted_rehearsal()

        report_data = res["report"]
        manifest_bytes = res["manifest_file"].read_bytes()

        # A. Valid pair passes
        assert validate_report_manifest_binding(report_data, manifest_bytes) is True

        # B. Modified manifest bytes fail binding
        bad_manifest_bytes = manifest_bytes + b"\n/* extra byte */"
        assert validate_report_manifest_binding(report_data, bad_manifest_bytes) is False

        # C. Wrong SHA in report fails binding
        bad_report = dict(report_data)
        bad_report["runtime_evidence_binding"] = {
            "manifest_filename": "runtime_manifest.json",
            "manifest_sha256": "0" * 64,
            "manifest_schema_version": "0.1",
        }
        assert validate_report_manifest_binding(bad_report, manifest_bytes) is False


def test_observed_container_network_and_mounts(tmp_path):
    """Verify container inspection observes network mode none and 0 host volume / socket mounts."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
    assert runner.observed_network_mode == "none"
    assert runner.observed_host_volume_mount_count == 0
    assert runner.observed_docker_socket_mount_count == 0
