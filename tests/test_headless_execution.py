"""Regression proof that normal AOS Runtime and worker execution produces zero visible console windows."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aos.process_utils import (
    background_python_executable,
    get_headless_creationflags,
    get_headless_startupinfo,
    popen_headless,
    process_alive,
    process_tree_snapshot,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows daemon interpreter contract")
def test_background_python_uses_pythonw_for_long_lived_daemons():
    expected = Path(sys.executable).with_name("pythonw.exe")

    if not expected.is_file():
        pytest.skip("pythonw.exe is not available beside the active interpreter")

    actual = Path(background_python_executable(sys.executable))

    assert actual.resolve() == expected.resolve()
    assert actual.name.lower() == "pythonw.exe"


def test_run_headless_forces_console_detach_on_windows(monkeypatch):
    """Bounded Windows CLIs must use the detached process contract."""
    import aos.process_utils as process_utils

    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, *args, **kwargs):
            return ("AOS_OK", "")

        def _close_job(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        process_utils,
        "popen_headless",
        fake_popen,
    )

    result = process_utils.run_headless(
        ["git", "--version"],
        timeout=1,
    )

    assert result.returncode == 0

    if os.name == "nt":
        assert captured.get("detached") is True
    else:
        assert captured.get("detached") is False


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows real Git console-detach acceptance",
)
def test_real_git_cli_detached_tree_has_no_conhost():
    """A live real git.exe tree must never acquire conhost.exe."""
    command = [
        "git",
        "-c",
        'alias.aos-hang=!python -c "import time; time.sleep(10)"',
        "aos-hang",
    ]

    proc = popen_headless(
        command,
        detached=True,
    )

    observed_live = False

    try:
        deadline = time.monotonic() + 2.0

        while time.monotonic() < deadline:
            if process_alive(proc.pid):
                observed_live = True

            tree = process_tree_snapshot([proc.pid])

            conhosts = [
                row
                for row in tree
                if str(row.get("name") or "").lower() == "conhost.exe"
            ]

            assert conhosts == []

            time.sleep(0.05)

        assert observed_live is True
    finally:
        proc.close()


def test_run_headless_executes_silently_and_captures_output():
    """Verify run_headless executes commands synchronously without window display and preserves output."""
    proc = run_headless([sys.executable, "-c", "import sys; print('AOS_HEADLESS_OK'); sys.stderr.write('AOS_DIAG_OK\\n')"], timeout=10)
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


def test_background_production_modules_do_not_call_subprocess_directly():
    repo_root = Path(__file__).resolve().parents[1]
    modules = (
        "extensions/autonomy-fabric/native_workers.py",
        "extensions/autonomy-fabric/antigravity_adapter.py",
        "extensions/autonomy-fabric/remote_source_evidence.py",
        "src/aos/git_workspace.py",
        "src/aos/execution_preflight.py",
        "src/aos/verification_workspace.py",
        "src/aos/candidate_store.py",
        "src/aos/workers/antigravity.py",
        "src/aos/workers/antigravity_probe.py",
        "src/aos/controlled_execution.py",
    )
    violations = []
    for relative_path in modules:
        path = repo_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
            ):
                violations.append(f"{relative_path}:{node.lineno}:{node.func.attr}")
    assert violations == []
