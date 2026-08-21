"""Antigravity WorkerAdapter implementation for AOS-3."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.validate import validate_document
from aos.workers.base import WorkerAdapter, WorkerExecutionResult

ADAPTER_CONTRACT_VERSION = "0.1.0"
SENSITIVE_ENV_VARS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}


def get_local_capability_store_path(adapter_name: str = "antigravity") -> Path:
    """Get machine-local OS-appropriate path for capability attestation storage."""
    custom_dir = os.environ.get("AOS_CAPABILITY_STORE_DIR")
    if custom_dir:
        return Path(custom_dir) / f"{adapter_name}.json"

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base_dir = Path(local_app_data) / "AOS" / "capabilities"
        else:
            base_dir = Path.home() / ".aos" / "capabilities"
    else:
        base_dir = Path.home() / ".aos" / "capabilities"

    return base_dir / f"{adapter_name}.json"


def compute_file_sha256(path: str | Path) -> str:
    """Compute SHA-256 hash of a local file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_reported_cli_version(
    cli_command: str = "agy",
    runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> Optional[str]:
    """Query CLI reported version."""
    try:
        if runner:
            res = runner([cli_command, "--version"])
        else:
            res = subprocess.run([cli_command, "--version"], capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return (res.stdout or "").strip()
        return None
    except Exception:
        return None


def resolve_capability_status(
    cli_command: str = "agy",
    store_path: Optional[Path] = None,
    version_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
) -> str:
    """Dynamically resolve machine-local capability status for the Antigravity CLI."""
    target_store = store_path or get_local_capability_store_path("antigravity")
    if not target_store.is_file():
        return "UNPROVEN"

    # Find current executable
    exe_path = shutil.which(cli_command) or (cli_command if os.path.isfile(cli_command) else None)
    if not exe_path or not os.path.isfile(exe_path):
        return "UNPROVEN"

    try:
        current_sha256 = compute_file_sha256(exe_path)
    except Exception:
        return "UNPROVEN"

    current_cli_version = get_reported_cli_version(cli_command, runner=version_runner)
    if not current_cli_version:
        return "UNPROVEN"

    try:
        with open(target_store, "r", encoding="utf-8") as f:
            attestation = json.load(f)

        val = validate_document("worker_capability_attestation", attestation)
        if not val.is_valid:
            return "UNPROVEN"

        if (
            attestation.get("worker_adapter") == "antigravity"
            and attestation.get("adapter_contract_version") == ADAPTER_CONTRACT_VERSION
            and attestation.get("executable_sha256") == current_sha256
            and attestation.get("reported_cli_version") == current_cli_version
            and attestation.get("capability_status") == "PROVEN"
        ):
            return "PROVEN"
        return "UNPROVEN"
    except Exception:
        return "UNPROVEN"


class AntigravityWorkerAdapter(WorkerAdapter):
    """WorkerAdapter implementation for Google Antigravity (agy CLI)."""

    def __init__(
        self,
        cli_command: str = "agy",
        runner: Optional[Callable[[List[str], str, int, Dict[str, str]], subprocess.CompletedProcess]] = None,
        capability_status_override: Optional[str] = None,
        store_path: Optional[Path] = None,
    ):
        self.cli_command = cli_command
        self.runner = runner or self._default_runner
        self.store_path = store_path
        if capability_status_override is not None:
            self.capability_status = capability_status_override
        else:
            self.capability_status = resolve_capability_status(self.cli_command, store_path=self.store_path)

    def _default_runner(self, cmd: List[str], cwd: str, timeout: int, env: Dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)

    def execute(
        self,
        task: Dict[str, Any],
        workspace_path: str,
        allowed_scope: Dict[str, Any],
        base_sha: str,
        timeout_seconds: int = 3600,
    ) -> WorkerExecutionResult:
        """Execute task inside isolated workspace using agy CLI with scrubbed environment."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = task.get("task_id", "UNKNOWN_TASK")
        title = task.get("title", "")
        desc = task.get("description", "")

        allowed_paths = allowed_scope.get("paths", [])
        forbidden_paths = allowed_scope.get("forbidden_paths", [])

        # Build secret-safe instruction prompt with explicit safety constraints
        prompt = (
            f"Task {task_id}: {title}\n"
            f"Description: {desc}\n"
            f"Base SHA: {base_sha}\n"
            f"Allowed Scope Paths: {allowed_paths}\n"
            f"Forbidden Scope Paths: {forbidden_paths}\n"
            "Execution Safety Rules:\n"
            "- Workspace is disposable and isolated.\n"
            "- Modify only allowed paths.\n"
            "- Do not modify forbidden paths.\n"
            "- Do not commit.\n"
            "- Do not push.\n"
            "- Do not merge.\n"
            "- Do not modify canonical project-control files unless explicitly allowed.\n"
            "- Do not access another repository.\n"
            "- Stop on ambiguity."
        )

        exe_path = shutil.which(self.cli_command) or self.cli_command
        workspace_dir = Path(workspace_path)

        cmd = [
            exe_path,
            "--print",
            "--dangerously-skip-permissions",
            "--mode", "accept-edits",
            "--add-dir", str(workspace_dir),
            prompt,
        ]

        # Scrub sensitive environment variables
        env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_VARS}

        exit_code = 0
        timed_out = False
        stdout_summary = ""
        stderr_summary = ""
        mutation_attempted = False

        try:
            res = self.runner(cmd, workspace_path, timeout_seconds, env)
            exit_code = res.returncode
            stdout_summary = (res.stdout or "")[:1000]
            stderr_summary = (res.stderr or "")[:1000]
            mutation_attempted = True
        except subprocess.TimeoutExpired as te:
            timed_out = True
            exit_code = None
            stdout_summary = (te.stdout or "")[:1000] if isinstance(te.stdout, str) else ""
            stderr_summary = (te.stderr or "")[:1000] if isinstance(te.stderr, str) else "Command timed out"
            mutation_attempted = True
        except Exception as e:
            exit_code = 1
            stderr_summary = str(e)[:1000]

        finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        return WorkerExecutionResult(
            worker_identity=f"antigravity-cli ({self.capability_status})",
            workspace_path=workspace_path,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            mutation_attempted=mutation_attempted,
            started_at=started_at,
            finished_at=finished_at,
        )
