"""Antigravity WorkerAdapter implementation for AOS-3."""

from __future__ import annotations

import datetime
import subprocess
from typing import Any, Callable, Dict, List, Optional

from aos.workers.base import WorkerAdapter, WorkerExecutionResult

CAPABILITY_STATUS = "ANTIGRAVITY_NONINTERACTIVE_CAPABILITY_UNPROVEN"


class AntigravityWorkerAdapter(WorkerAdapter):
    """WorkerAdapter implementation for Google Antigravity (agy CLI)."""

    def __init__(
        self,
        cli_command: str = "agy",
        runner: Optional[Callable[[List[str], str, int], subprocess.CompletedProcess]] = None,
    ):
        self.cli_command = cli_command
        self.runner = runner or self._default_runner

    def _default_runner(self, cmd: List[str], cwd: str, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    def execute(
        self,
        task: Dict[str, Any],
        workspace_path: str,
        allowed_scope: Dict[str, Any],
        base_sha: str,
        timeout_seconds: int = 3600,
    ) -> WorkerExecutionResult:
        """Execute task inside isolated workspace using agy CLI."""
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = task.get("task_id", "UNKNOWN_TASK")
        title = task.get("title", "")
        desc = task.get("description", "")

        # Build secret-safe instruction prompt
        prompt = f"Task {task_id}: {title}\nDescription: {desc}\nBase SHA: {base_sha}\nAllowed Scope: {allowed_scope.get('paths', [])}"

        cmd = [
            self.cli_command,
            "--print",
            "--mode", "accept-edits",
            "--add-dir", workspace_path,
            "--prompt", prompt,
        ]

        exit_code = 0
        timed_out = False
        stdout_summary = ""
        stderr_summary = ""
        mutation_attempted = False

        try:
            res = self.runner(cmd, workspace_path, timeout_seconds)
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
            worker_identity=f"antigravity-cli ({CAPABILITY_STATUS})",
            workspace_path=workspace_path,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_summary=stdout_summary,
            stderr_summary=stderr_summary,
            mutation_attempted=mutation_attempted,
            started_at=started_at,
            finished_at=finished_at,
        )
