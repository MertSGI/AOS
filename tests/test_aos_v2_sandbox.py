"""Platform and conformance tests for AOS V2 OS execution sandbox."""
import os
import sys
from pathlib import Path

import pytest

from aos.v2.sandbox import (
    SandboxPreflightResult,
    SandboxRunner,
    SandboxTestResult,
    SandboxUnavailableError,
    get_available_ram_bytes,
    preflight_sandbox,
)


@pytest.fixture(autouse=True)
def mock_adequate_ram(monkeypatch):
    """Ensure host available RAM threshold is satisfied during sandbox functional tests."""
    orig = get_available_ram_bytes()
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: max(orig, 2 * 1024 * 1024 * 1024))


def test_sandbox_preflight_reports_valid_state():
    res = preflight_sandbox()
    assert isinstance(res, SandboxPreflightResult)
    assert res.platform in ("windows", "linux", "unsupported")
    assert res.available_ram_bytes >= 0
    if sys.platform.startswith("win"):
        assert res.isolation_mechanism == "WINDOWS_APPCONTAINER_JOB_OBJECT"
    elif sys.platform.startswith("linux"):
        assert res.isolation_mechanism == "LINUX_BUBBLEWRAP"


def test_sandbox_runs_trusted_python_code(tmp_path):
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "test_module.py").write_text("def test_func(): return 42\n")

    scratch = tmp_path / "scratch"
    code = "import test_module; res = test_module.test_func(); assert res == 42; print('SUCCESS_42')"

    result = runner.run_test(
        [sys.executable, "-B", "-c", code],
        candidate_dir=candidate,
        scratch_dir=scratch,
        timeout=10.0,
    )

    assert isinstance(result, SandboxTestResult)
    assert result.exit_code == 0
    assert "SUCCESS_42" in result.stdout
    assert not result.timed_out


def test_sandbox_denies_outside_writes(tmp_path):
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside_target = tmp_path / "outside.txt"

    scratch = tmp_path / "scratch"
    code = f"with open(r'{outside_target}', 'w') as f: f.write('escape')"

    result = runner.run_test(
        [sys.executable, "-I", "-B", "-c", code],
        candidate_dir=candidate,
        scratch_dir=scratch,
        timeout=10.0,
    )

    # Must fail with PermissionError or non-zero exit code
    assert result.exit_code != 0
    assert not outside_target.exists()


def test_sandbox_denies_loopback_and_network_connect(tmp_path):
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    scratch = tmp_path / "scratch"

    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(2.0)\n"
        "s.connect(('127.0.0.1', 80))\n"
    )

    result = runner.run_test(
        [sys.executable, "-I", "-B", "-c", code],
        candidate_dir=candidate,
        scratch_dir=scratch,
        timeout=10.0,
    )

    # Connecting to network/loopback without capability must fail
    assert result.exit_code != 0


def test_sandbox_denies_child_process_creation(tmp_path):
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    scratch = tmp_path / "scratch"

    code = (
        "import subprocess, sys\n"
        "p = subprocess.run([sys.executable, '-c', 'print(\"child\")'], capture_output=True)\n"
        "assert p.returncode == 0\n"
    )

    result = runner.run_test(
        [sys.executable, "-I", "-B", "-c", code],
        candidate_dir=candidate,
        scratch_dir=scratch,
        timeout=10.0,
    )

    # Job Object active process limit = 1 blocks spawning child process
    assert result.exit_code != 0


def test_sandbox_enforces_timeout(tmp_path):
    preflight = preflight_sandbox()
    if not preflight.is_supported:
        pytest.skip(f"Sandbox unsupported on host: {'; '.join(preflight.reasons)}")

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    scratch = tmp_path / "scratch"

    code = "import time; time.sleep(10)"

    result = runner.run_test(
        [sys.executable, "-I", "-B", "-c", code],
        candidate_dir=candidate,
        scratch_dir=scratch,
        timeout=2.0,  # 2 second timeout
    )

    assert result.timed_out is True
    assert result.exit_code == 124


def test_sandbox_fails_closed_when_unsupported(monkeypatch, tmp_path):
    # Simulate unavailable RAM (< 1 GiB)
    monkeypatch.setattr("aos.v2.sandbox.get_available_ram_bytes", lambda: 500 * 1024 * 1024)
    res = preflight_sandbox()
    assert res.status == "EXECUTION_ISOLATION_UNAVAILABLE"
    assert not res.is_supported

    runner = SandboxRunner()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    scratch = tmp_path / "scratch"

    with pytest.raises(SandboxUnavailableError, match="EXECUTION_ISOLATION_UNAVAILABLE"):
        runner.run_test([sys.executable, "-c", "print(1)"], candidate, scratch)
