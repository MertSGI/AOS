"""Antigravity WorkerAdapter implementation for AOS-3."""

from __future__ import annotations

import datetime
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional

from aos.workers.base import WorkerAdapter, WorkerExecutionResult

CAPABILITY_STATUS = "UNPROVEN"
SENSITIVE_ENV_VARS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}


class AntigravityWorkerAdapter(WorkerAdapter):
    """WorkerAdapter implementation for Google Antigravity (agy CLI)."""

    capability_status: str = CAPABILITY_STATUS

    def __init__(
        self,
        cli_command: str = "agy",
        runner: Optional[Callable[[List[str], str, int, Dict[str, str]], subprocess.CompletedProcess]] = None,
    ):
        self.cli_command = cli_command
        self.runner = runner or self._default_runner

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

        cmd = [
            self.cli_command,
            "--print",
            "--mode", "accept-edits",
            "--add-dir", workspace_path,
            "--prompt", prompt,
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
