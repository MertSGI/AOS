"""Regression proof that normal AOS Runtime and worker execution produces zero visible console windows."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aos.process_utils import (
    get_headless_creationflags,
    get_headless_startupinfo,
    popen_headless,
    run_headless,
)


def test_headless_process_flags_on_windows():
    """Verify creationflags and startupinfo on Windows vs POSIX."""
    if os.name == "nt":
        sync_flags = get_headless_creationflags(detached=False)
        assert sync_flags & 0x08000000 != 0  # CREATE_NO_WINDOW

        detached_flags = get_headless_creationflags(detached=True)
        assert detached_flags & 0x08000000 != 0  # CREATE_NO_WINDOW
        assert detached_flags & 0x00000008 != 0  # DETACHED_PROCESS
        assert detached_flags & 0x00000200 != 0  # CREATE_NEW_PROCESS_GROUP

        si = get_headless_startupinfo()
        assert si is not None
        assert si.dwFlags & 1 != 0  # STARTF_USESHOWWINDOW
        assert si.wShowWindow == 0  # SW_HIDE
    else:
        assert get_headless_creationflags(detached=False) == 0
        assert get_headless_creationflags(detached=True) == 0
        assert get_headless_startupinfo() is None


def test_run_headless_executes_silently_and_captures_output():
    """Verify run_headless executes commands synchronously without window display and preserves output."""
    proc = run_headless([sys.executable, "-c", "import sys; print('AOS_HEADLESS_OK'); sys.stderr.write('AOS_DIAG_OK\\n')"])
    assert proc.returncode == 0
    assert "AOS_HEADLESS_OK" in proc.stdout
    assert "AOS_DIAG_OK" in proc.stderr


def test_popen_headless_detached_worker():
    """Verify popen_headless detached process creation configures windowless flags."""
    proc = popen_headless(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        detached=True,
    )
    assert proc.pid > 0
    proc.wait(timeout=10)
    assert proc.returncode == 0


def test_runtime_server_worker_spawning_uses_headless_flags(tmp_path, monkeypatch):
    """Verify RuntimeEngine._spawn_worker configures headless popen without window flashing."""
    from aos.runtime_server import RuntimeEngine
    from aos.runtime_contract import ProjectProfile, ContinueProjectCommand

    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    captured_kwargs = {}

    def mock_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        # Mock minimal process
        class FakeProc:
            pid = 88888
        return FakeProc()

    monkeypatch.setattr("aos.runtime_server.popen_headless", mock_popen)
    engine = RuntimeEngine(cfg)
    try:
        profile = ProjectProfile(
            project_id="lari",
            descriptor_path=str(descriptor),
            workspace=str(workspace),
            routing_policy_path=str(policy),
        )
        cmd = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)
        engine.store.create_command(cmd.to_dict())
        engine._spawn_worker(cmd.command_id, recovered=False)

        assert captured_kwargs.get("detached") is True
        assert captured_kwargs.get("stdin") == subprocess.DEVNULL
        assert captured_kwargs.get("stdout") == subprocess.DEVNULL
        assert captured_kwargs.get("stderr") == subprocess.DEVNULL
    finally:
        engine.shutdown()
