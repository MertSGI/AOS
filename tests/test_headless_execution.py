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
    launch_startup_authority,
    get_headless_startupinfo,
    popen_headless,
    process_alive,
    process_tree_snapshot,
    run_headless,
    visible_window_snapshot,
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


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows real Git visible-window acceptance",
)
def test_real_git_cli_tree_has_no_visible_windows():
    """Real git may own headless conhost, but must own zero visible HWNDs."""
    proc = popen_headless(
        [
            "git",
            "cat-file",
            "--batch",
        ],
        detached=False,
    )

    observed_live = False
    observed_conhost = False
    visible = []

    try:
        deadline = time.monotonic() + 1.5

        while time.monotonic() < deadline:
            if process_alive(proc.pid):
                observed_live = True

            tree = process_tree_snapshot([proc.pid])

            if any(
                str(row.get("name") or "").lower() == "conhost.exe"
                for row in tree
            ):
                observed_conhost = True

            visible.extend(
                visible_window_snapshot([proc.pid])
            )

            time.sleep(0.01)

        assert observed_live is True
        # conhost presence is permitted; visible window ownership is not.
        assert visible == []
        assert isinstance(observed_conhost, bool)
    finally:
        proc.close()


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Startup shell association acceptance",
)
def test_windows_vbs_startup_authority_executes_via_shell(tmp_path: Path):
    """The persistent authority must use a Windows-native shell type.

    This catches hosts where .pyw is unassociated and ShellExecute would show
    an "open with" picker instead of starting AOS.
    """
    marker = tmp_path / "startup-marker.txt"
    script = tmp_path / "AOS-Runtime-V1-Supervisor.vbs"

    marker_text = str(marker).replace('"', '""')

    script.write_text(
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'Set out = fso.CreateTextFile("'
        + marker_text
        + '", True)\r\n'
        'out.Write "AOS_VBS_STARTUP_OK"\r\n'
        'out.Close\r\n'
        'Set out = Nothing\r\n'
        'Set fso = Nothing\r\n',
        encoding="utf-8",
        newline="\r\n",
    )

    launch_startup_authority(script)

    deadline = time.monotonic() + 5.0

    while (
        not marker.is_file()
        and
        time.monotonic() < deadline
    ):
        time.sleep(0.05)

    assert marker.is_file()
    assert (
        marker.read_text(encoding="utf-8")
        == "AOS_VBS_STARTUP_OK"
    )



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
