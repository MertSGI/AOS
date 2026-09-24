"""Deterministic tests for LlamaCppLifecycleManager."""
from __future__ import annotations

import subprocess
import time
from unittest.mock import MagicMock, patch
import pytest

from aos.workers.llama_cpp_lifecycle import (
    LlamaCppLifecycleManager,
    QwenLifecycleState,
)


class FakeOwnedProcess:
    def __init__(self, pid: int = 12345, returncode: int | None = None):
        self._pid = pid
        self._returncode = returncode
        self.terminated = False
        self.closed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self._returncode

    def terminate_tree(self, exit_code: int = 1) -> None:
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout: float = 5.0) -> int:
        return 0

    def kill(self) -> None:
        self._returncode = -9

    def close(self) -> None:
        self.closed = True


def test_initial_state_is_stopped_ready():
    mgr = LlamaCppLifecycleManager()
    snap = mgr.get_snapshot()
    assert snap.state == QwenLifecycleState.STOPPED_READY
    assert snap.pid is None
    assert snap.port == 8080
    assert snap.host == "127.0.0.1"


def test_non_loopback_host_rejected():
    with pytest.raises(ValueError, match="exclusively to loopback"):
        LlamaCppLifecycleManager(host="192.168.1.100")


def test_qwen_auto_start_and_health_readiness():
    fake_proc = FakeOwnedProcess(pid=9999)
    spawner = MagicMock(return_value=fake_proc)

    with patch("aos.workers.llama_cpp_lifecycle.resolve_llama_cpp_identity") as mock_exe, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_qwen_model") as mock_model, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_capability_status", return_value="PROVEN"), \
         patch("extensions.autonomy_fabric.llama_cpp_reasoning_backend.build_llama_server_argv", return_value=["llama-server.exe"]):

        mock_exe.return_value = {"path": "C:\\path\\llama-server.exe", "sha256": "abc", "version": "1.0"}
        mock_model.return_value = {"path": "C:\\path\\model.gguf", "sha256": "def", "size_bytes": 100}

        mgr = LlamaCppLifecycleManager(
            executable_path="C:\\path\\llama-server.exe",
            model_path="C:\\path\\model.gguf",
            startup_timeout=2.0,
            spawner=spawner,
        )

        with patch.object(mgr, "check_health", return_value=True):
            snap = mgr.start()
            assert snap.state == QwenLifecycleState.AVAILABLE
            assert snap.pid == 9999
            assert spawner.called


def test_qwen_active_request_transitions_and_idle_timeout():
    fake_proc = FakeOwnedProcess(pid=9999)
    mgr = LlamaCppLifecycleManager(idle_timeout=0.2)
    mgr._process = fake_proc
    mgr._state = QwenLifecycleState.AVAILABLE

    # Request started -> BUSY
    mgr.mark_request_started()
    assert mgr.get_snapshot().state == QwenLifecycleState.BUSY

    # Request finished -> AVAILABLE
    mgr.mark_request_finished()
    assert mgr.get_snapshot().state == QwenLifecycleState.AVAILABLE

    # Before idle timeout -> not stopped
    assert not mgr.check_idle_timeout()

    # Wait for idle timeout
    time.sleep(0.25)
    stopped = mgr.check_idle_timeout()
    assert stopped
    assert mgr.get_snapshot().state == QwenLifecycleState.STOPPED_READY
    assert fake_proc.terminated
    assert mgr.get_snapshot().pid is None


def test_qwen_restart():
    fake_proc1 = FakeOwnedProcess(pid=1001)
    fake_proc2 = FakeOwnedProcess(pid=1002)
    spawner = MagicMock(side_effect=[fake_proc1, fake_proc2])

    with patch("aos.workers.llama_cpp_lifecycle.resolve_llama_cpp_identity") as mock_exe, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_qwen_model") as mock_model, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_capability_status", return_value="PROVEN"), \
         patch("extensions.autonomy_fabric.llama_cpp_reasoning_backend.build_llama_server_argv", return_value=["llama-server.exe"]):

        mock_exe.return_value = {"path": "C:\\path\\llama-server.exe", "sha256": "abc", "version": "1.0"}
        mock_model.return_value = {"path": "C:\\path\\model.gguf", "sha256": "def", "size_bytes": 100}

        mgr = LlamaCppLifecycleManager(
            executable_path="C:\\path\\llama-server.exe",
            model_path="C:\\path\\model.gguf",
            startup_timeout=2.0,
            spawner=spawner,
        )

        with patch.object(mgr, "check_health", return_value=True):
            snap1 = mgr.start()
            assert snap1.state == QwenLifecycleState.AVAILABLE
            assert snap1.pid == 1001

            mgr.stop()
            assert fake_proc1.terminated

            snap2 = mgr.start()
            assert snap2.state == QwenLifecycleState.AVAILABLE
            assert snap2.pid == 1002


def test_qwen_routed_execution_and_orphan_count(tmp_path):
    import json
    from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest
    from extensions.autonomy_fabric.llama_cpp_reasoning_backend import LlamaCppQwenReasoningBackend

    fake_proc = FakeOwnedProcess(pid=7777)
    spawner = MagicMock(return_value=fake_proc)

    with patch("aos.workers.llama_cpp_lifecycle.resolve_llama_cpp_identity") as mock_exe, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_qwen_model") as mock_model, \
         patch("aos.workers.llama_cpp_lifecycle.resolve_capability_status", return_value="PROVEN"), \
         patch("extensions.autonomy_fabric.llama_cpp_reasoning_backend.build_llama_server_argv", return_value=["llama-server.exe"]):

        mock_exe.return_value = {"path": "C:\\path\\llama-server.exe", "sha256": "abc", "version": "1.0"}
        mock_model.return_value = {"path": "C:\\path\\model.gguf", "sha256": "def", "size_bytes": 100}

        mgr = LlamaCppLifecycleManager(
            executable_path="C:\\path\\llama-server.exe",
            model_path="C:\\path\\model.gguf",
            startup_timeout=2.0,
            spawner=spawner,
        )

        def mock_transport(payload, timeout):
            return {
                "choices": [{"message": {"content": json.dumps({"class": "safe", "confidence": 0.95})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

        backend = LlamaCppQwenReasoningBackend(
            capability_status_provider=lambda: "PROVEN",
            health_reader=lambda: True,
            completion_transport=mock_transport,
            lifecycle_manager=mgr,
        )

        schema = {
            "type": "object",
            "required": ["class", "confidence"],
            "properties": {"class": {"type": "string"}, "confidence": {"type": "number"}},
        }
        request = ExecutionRequest(
            task_id="task-qwen-1",
            project_id="p1",
            workspace=str(tmp_path),
            operation_class="REASONING",
            required_capabilities=[ExecutionCapability.MODEL_REASONING],
            authority_id="A1",
            payload={"prompt": "Test prompt", "schema": schema},
        )

        with patch.object(mgr, "check_health", return_value=True):
            # Initially stopped
            assert mgr.get_snapshot().state == QwenLifecycleState.STOPPED_READY

            # Execute triggers auto-start and finishes successfully
            res = backend.execute(request)
            assert res.status == "SUCCESS"
            assert res.transient_structured_output == {"class": "safe", "confidence": 0.95}

            # State is back to AVAILABLE after execution finishes
            snap = mgr.get_snapshot()
            assert snap.state == QwenLifecycleState.AVAILABLE
            assert snap.orphan_count == 0
