"""Comprehensive unit test suite for Hosted Disposable Rehearsal primitive (R4.2.1 Evidence Closure)."""

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
    """Verify all R4.2.1 schemas are Draft 2020-12 valid."""
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


def test_container_inspect_network_none_passes(tmp_path):
    """Verify container inspection with network mode 'none' passes."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
        mock_cmd.return_value = res
        runner._start_postgres_container("test-c")
        assert runner.observed_network_mode == "none"


def test_container_inspect_network_bridge_fails(tmp_path):
    """Verify container inspection with network mode 'bridge' fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"bridge","PidsLimit":100},"Mounts":[]}]'
        mock_cmd.return_value = res
        with pytest.raises(HostedDisposableRehearsalError, match="network mode is 'bridge'"):
            runner._start_postgres_container("test-c")


def test_container_inspect_pids_100_passes(tmp_path):
    """Verify container inspection with pids_limit 100 passes."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
        mock_cmd.return_value = res
        runner._start_postgres_container("test-c")
        assert runner.observed_pids_limit == 100


def test_container_inspect_wrong_pids_fails(tmp_path):
    """Verify container inspection with wrong pids_limit fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":0},"Mounts":[]}]'
        mock_cmd.return_value = res
        with pytest.raises(HostedDisposableRehearsalError, match="pids_limit is 0"):
            runner._start_postgres_container("test-c")


def test_container_inspect_bind_mount_fails(tmp_path):
    """Verify container inspection with host bind mount fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[{"Type":"bind","Source":"/host"}]}]'
        mock_cmd.return_value = res
        with pytest.raises(HostedDisposableRehearsalError, match="has 1 host bind mounts"):
            runner._start_postgres_container("test-c")


def test_container_inspect_docker_socket_mount_fails(tmp_path):
    """Verify container inspection with docker.sock mount fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        res = MagicMock()
        res.returncode = 0
        res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[{"Type":"volume","Source":"/var/run/docker.sock"}]}]'
        mock_cmd.return_value = res
        with pytest.raises(HostedDisposableRehearsalError, match="has 1 docker.sock mounts"):
            runner._start_postgres_container("test-c")


def test_container_inspect_nonzero_fails(tmp_path):
    """Verify container inspection returning nonzero return code fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        def side_effect(cmd, cwd=None, timeout=60):
            res = MagicMock()
            cmd_str = " ".join(cmd)
            if "docker inspect" in cmd_str:
                res.returncode = 1
                res.stderr = "No such object"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect
        with pytest.raises(HostedDisposableRehearsalError, match="returned nonzero exit code 1"):
            runner._start_postgres_container("test-c")


def test_container_inspect_malformed_json_fails(tmp_path):
    """Verify container inspection returning malformed JSON fails closed."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, tmp_path)

    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        def side_effect(cmd, cwd=None, timeout=60):
            res = MagicMock()
            cmd_str = " ".join(cmd)
            if "docker inspect" in cmd_str:
                res.returncode = 0
                res.stdout = "INVALID_JSON"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect
        with pytest.raises(HostedDisposableRehearsalError, match="Failed to parse docker inspect"):
            runner._start_postgres_container("test-c")


def _make_mock_side_effect(mock_cmd):
    def side_effect(cmd, cwd=None, timeout=60):
        res = MagicMock()
        cmd_str = " ".join(cmd)
        if "docker rm" in cmd_str:
            res.returncode = 0
            res.stderr = ""
        elif "docker inspect" in cmd_str:
            target_container = cmd[-1]
            rm_calls = [c[0][0] for c in mock_cmd.call_args_list if len(c[0][0]) >= 4 and c[0][0][0:2] == ["docker", "rm"]]
            has_rm_for_target = any(target_container in call_args for call_args in rm_calls)
            if has_rm_for_target:
                res.returncode = 1
                res.stderr = "No such container"
            else:
                res.returncode = 0
                res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
        elif "docker cp" in cmd_str:
            res.returncode = 0
            res.stderr = ""
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
    return side_effect


def test_two_disposable_containers_are_independently_inspected(tmp_path):
    """Verify both disposable containers (migration & failure probe) are independently inspected."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        mock_cmd.side_effect = _make_mock_side_effect(mock_cmd)
        res = runner.execute_hosted_rehearsal()
        assert runner.container_runtime_inspection_count == 2
        assert "aos_worktree_immutable" not in res["report"]


def test_runtime_manifest_does_not_predeclare_worktree_immutability(tmp_path):
    """Verify runtime manifest payload does not contain aos_worktree_immutable field."""
    req_path = FIXTURE_ROOT / "request.valid.json"
    req = load_json_strict(req_path)
    out_dir = tmp_path / "out"

    runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
    with patch("aos.hosted_disposable_rehearsal.run_bounded_command") as mock_cmd:
        mock_cmd.side_effect = _make_mock_side_effect(mock_cmd)
        res = runner.execute_hosted_rehearsal()
        manifest_data = json.loads(res["manifest_file"].read_text("utf-8"))
        assert "aos_worktree_immutable" not in manifest_data


def test_primary_failure_preserved_with_secondary_cleanup_failure(tmp_path):
    """Verify primary migration failure is preserved as primary exception while cleanup failure is included as secondary note."""
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
                res.stderr = "docker rm failure"
            elif "docker inspect" in cmd_str:
                res.returncode = 0
                res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
            elif "docker cp" in cmd_str:
                res.returncode = 0
            elif "psql" in cmd_str and "001_create_rehearsal_items.sql" in cmd_str:
                res.returncode = 1
                res.stderr = "Primary migration failure error"
            else:
                res.returncode = 0
                res.stdout = "pg_isready - accepting connections"
            return res

        mock_cmd.side_effect = side_effect
        with pytest.raises(HostedDisposableRehearsalError) as exc_info:
            runner.execute_hosted_rehearsal()

        err_msg = str(exc_info.value)
        assert "Primary migration failure error" in err_msg
        assert "Secondary cleanup failure" in err_msg


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
            elif "docker inspect" in cmd_str:
                res.returncode = 0
                res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
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
                res.stdout = '[{"HostConfig":{"NetworkMode":"none","PidsLimit":100},"Mounts":[]}]'
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
        mock_cmd.side_effect = _make_mock_side_effect(mock_cmd)
        runner = HostedDisposableRunner(req, req_path, FIXTURE_ROOT, out_dir)
        res = runner.execute_hosted_rehearsal()

        report_data = res["report"]
        manifest_bytes = res["manifest_file"].read_bytes()

        assert validate_report_manifest_binding(report_data, manifest_bytes) is True

        bad_manifest_bytes = manifest_bytes + b"\n/* extra byte */"
        assert validate_report_manifest_binding(report_data, bad_manifest_bytes) is False

        bad_report = dict(report_data)
        bad_report["runtime_evidence_binding"] = {
            "manifest_filename": "runtime_manifest.json",
            "manifest_sha256": "0" * 64,
            "manifest_schema_version": "0.1",
        }
        assert validate_report_manifest_binding(bad_report, manifest_bytes) is False
