"""Resource OS managed on-demand lifecycle for local Qwen3-4B via llama.cpp.

Provides deterministic loopback binding, single-instance verification,
orphan cleanup, bounded startup health readiness, idle timeout termination,
and cockpit lifecycle states.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.process_utils import OwnedProcess, popen_headless, process_alive, terminate_process_tree
from aos.workers.llama_cpp_probe import (
    capability_store_path,
    resolve_capability_status,
    resolve_llama_cpp_identity,
    resolve_qwen_model,
)

logger = logging.getLogger("aos.llama_cpp_lifecycle")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_IDLE_TIMEOUT = 300.0  # 5 minutes idle timeout before stopping to free ~5 GB RAM


class QwenLifecycleState(str, Enum):
    STOPPED_READY = "STOPPED_READY"
    STARTING = "STARTING"
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    IDLE = "IDLE"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class QwenLifecycleSnapshot:
    state: QwenLifecycleState
    host: str
    port: int
    pid: Optional[int]
    orphan_count: int
    idle_seconds: float
    last_health_status: str
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "orphan_count": self.orphan_count,
            "idle_seconds": round(self.idle_seconds, 1),
            "last_health_status": self.last_health_status,
            "error_message": self.error_message,
        }


class LlamaCppLifecycleManager:
    """Manages the on-demand lifecycle of llama-server.exe running Qwen3-4B."""

    def __init__(
        self,
        *,
        executable_path: Optional[str] = None,
        model_path: Optional[str] = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        spawner: Optional[Callable[..., Any]] = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("llama.cpp lifecycle manager must bind exclusively to loopback")
        self.host = host
        self.port = port
        self.startup_timeout = startup_timeout
        self.idle_timeout = idle_timeout
        self._spawner = spawner or popen_headless
        self._executable_path = executable_path or os.environ.get("AOS_LLAMA_CPP_EXECUTABLE")
        self._model_path = model_path or os.environ.get("AOS_QWEN_MODEL_PATH")
        self._process: Optional[OwnedProcess] = None
        self._state = QwenLifecycleState.STOPPED_READY
        self._last_active_time = time.monotonic()
        self._active_requests = 0
        self._lock = threading.Lock()
        self._error_message: Optional[str] = None
        self._last_health = "unknown"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def check_health(self) -> bool:
        """Poll /health endpoint on the loopback server."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health", headers={"User-Agent": "AOS-Qwen-Lifecycle/1.0"})
            with urllib.request.urlopen(req, timeout=1.5) as response:
                if response.status != 200:
                    self._last_health = f"http_{response.status}"
                    return False
                body = response.read(64 * 1024)
            data = json.loads(body.decode("utf-8"))
            status = data.get("status")
            self._last_health = str(status)
            return status in {"ok", "ready"}
        except Exception as exc:
            self._last_health = f"error_{type(exc).__name__}"
            return False

    def clean_orphans(self) -> int:
        """Find and terminate any existing orphan llama-server.exe processes on the host."""
        if os.name != "nt":
            return 0
        cleaned = 0
        try:
            import ctypes
            from ctypes import wintypes
            from aos.process_utils import PROCESSENTRY32W

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot in (0, -1):
                return 0
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(entry)
                ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                our_pid = self._process.pid if self._process else None
                while ok:
                    name = str(entry.szExeFile).lower()
                    pid = int(entry.th32ProcessID)
                    if name == "llama-server.exe" and pid != our_pid and pid != os.getpid():
                        terminate_process_tree(pid)
                        cleaned += 1
                    ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception as exc:
            logger.warning("Error scanning for orphan llama-server processes: %s", exc)
        return cleaned

    def start(self) -> QwenLifecycleSnapshot:
        with self._lock:
            # If already running and healthy, transition state if needed and return
            if self._process is not None and self._process.poll() is None:
                if self.check_health():
                    self._state = QwenLifecycleState.AVAILABLE if self._active_requests == 0 else QwenLifecycleState.BUSY
                    return self._snapshot_locked()
                # Process exists but health failed
                self.stop_locked()

            self._state = QwenLifecycleState.STARTING
            self._error_message = None
            cleaned = self.clean_orphans()

            # Resolve paths
            exe_identity = resolve_llama_cpp_identity(self._executable_path or "llama-server")
            if not exe_identity:
                self._state = QwenLifecycleState.FAILED
                self._error_message = "llama-server executable not resolved or invalid"
                return self._snapshot_locked(orphan_count=cleaned)

            model_identity = resolve_qwen_model(self._model_path) if self._model_path else None
            if not model_identity:
                self._state = QwenLifecycleState.FAILED
                self._error_message = "Qwen3-4B Q4_K_M GGUF model not resolved or invalid"
                return self._snapshot_locked(orphan_count=cleaned)

            cap_status = resolve_capability_status(
                executable=exe_identity,
                model=model_identity,
                store_path=capability_store_path(),
            )
            if cap_status != "PROVEN":
                self._state = QwenLifecycleState.FAILED
                self._error_message = f"Qwen capability status is {cap_status}, not PROVEN"
                return self._snapshot_locked(orphan_count=cleaned)

            # Build exact argv
            try:
                from extensions.autonomy_fabric.llama_cpp_reasoning_backend import build_llama_server_argv
            except ImportError:
                import importlib.util
                candidate_path = Path(__file__).resolve().parent.parent.parent.parent / "extensions" / "autonomy-fabric" / "llama_cpp_reasoning_backend.py"
                if candidate_path.exists():
                    spec = importlib.util.spec_from_file_location("llama_cpp_reasoning_backend", candidate_path)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    build_llama_server_argv = mod.build_llama_server_argv
                else:
                    def build_llama_server_argv(executable: str, model_path: str, *, port: int = 8080) -> list[str]:
                        return [
                            executable, "--model", str(Path(model_path).resolve()),
                            "--host", "127.0.0.1", "--port", str(port),
                            "--ctx-size", "4096", "--n-predict", "512",
                            "--threads", str(min(8, os.cpu_count() or 1)),
                            "--n-gpu-layers", "0",
                            "--parallel", "1", "--batch-size", "128", "--ubatch-size", "128",
                            "--no-webui", "--jinja",
                        ]
            argv = build_llama_server_argv(exe_identity["path"], model_identity["path"], port=self.port)

            try:
                self._process = self._spawner(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                self._state = QwenLifecycleState.FAILED
                self._error_message = f"Process spawn failed: {exc}"
                return self._snapshot_locked(orphan_count=cleaned)

            # Poll for readiness
            deadline = time.monotonic() + self.startup_timeout
            started = False
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self._state = QwenLifecycleState.FAILED
                    self._error_message = f"llama-server exited prematurely with code {self._process.poll()}"
                    self.stop_locked()
                    return self._snapshot_locked(orphan_count=cleaned)
                if self.check_health():
                    started = True
                    break
                time.sleep(0.5)

            if not started:
                self._state = QwenLifecycleState.FAILED
                self._error_message = f"llama-server readiness timed out after {self.startup_timeout}s"
                self.stop_locked()
                return self._snapshot_locked(orphan_count=cleaned)

            self._state = QwenLifecycleState.AVAILABLE
            self._last_active_time = time.monotonic()
            return self._snapshot_locked(orphan_count=cleaned)

    def stop_locked(self) -> None:
        if self._process is not None:
            self._state = QwenLifecycleState.STOPPING
            try:
                self._process.terminate_tree()
                try:
                    self._process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5.0)
            except Exception:
                pass
            finally:
                if hasattr(self._process, "close"):
                    self._process.close()
                self._process = None
        self._state = QwenLifecycleState.STOPPED_READY

    def stop(self) -> QwenLifecycleSnapshot:
        with self._lock:
            self.stop_locked()
            return self._snapshot_locked()

    def mark_request_started(self) -> None:
        with self._lock:
            self._active_requests += 1
            self._state = QwenLifecycleState.BUSY
            self._last_active_time = time.monotonic()

    def mark_request_finished(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_active_time = time.monotonic()
            if self._active_requests == 0:
                self._state = QwenLifecycleState.AVAILABLE

    def check_idle_timeout(self) -> bool:
        """If idle beyond idle_timeout and no active requests, shut down to free ~5GB RAM."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if self._active_requests == 0:
                    idle_duration = time.monotonic() - self._last_active_time
                    if idle_duration >= self.idle_timeout:
                        logger.info("Stopping Qwen llama-server after %.1fs idle", idle_duration)
                        self.stop_locked()
                        return True
                    else:
                        self._state = QwenLifecycleState.IDLE if idle_duration > 15.0 else QwenLifecycleState.AVAILABLE
            return False

    def get_snapshot(self) -> QwenLifecycleSnapshot:
        with self._lock:
            # Refresh liveness
            if self._process is not None:
                if self._process.poll() is not None:
                    self._state = QwenLifecycleState.FAILED
                    self._error_message = f"Process terminated unexpectedly with code {self._process.poll()}"
                    self.stop_locked()
                elif self._active_requests == 0:
                    idle_duration = time.monotonic() - self._last_active_time
                    if idle_duration > 15.0 and self._state == QwenLifecycleState.AVAILABLE:
                        self._state = QwenLifecycleState.IDLE
            return self._snapshot_locked()

    def _snapshot_locked(self, orphan_count: int = 0) -> QwenLifecycleSnapshot:
        idle_duration = time.monotonic() - self._last_active_time if self._process else 0.0
        pid = self._process.pid if self._process and self._process.poll() is None else None
        return QwenLifecycleSnapshot(
            state=self._state,
            host=self.host,
            port=self.port,
            pid=pid,
            orphan_count=orphan_count,
            idle_seconds=idle_duration,
            last_health_status=self._last_health,
            error_message=self._error_message,
        )
