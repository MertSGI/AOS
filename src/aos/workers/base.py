"""Base WorkerAdapter interface for AOS-3."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class WorkerExecutionResult:
    """Structured result returned by a WorkerAdapter execution."""

    def __init__(
        self,
        worker_identity: str,
        workspace_path: str,
        exit_code: Optional[int],
        timed_out: bool,
        stdout_summary: str,
        stderr_summary: str,
        mutation_attempted: bool,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ):
        self.worker_identity = worker_identity
        self.workspace_path = workspace_path
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.stdout_summary = stdout_summary
        self.stderr_summary = stderr_summary
        self.mutation_attempted = mutation_attempted
        self.started_at = started_at
        self.finished_at = finished_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "worker_identity": self.worker_identity,
            "workspace_path": self.workspace_path,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_summary": self.stdout_summary,
            "stderr_summary": self.stderr_summary,
            "mutation_attempted": self.mutation_attempted,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class WorkerAdapter(ABC):
    """Abstract base class for AOS worker adapters."""

    capability_status: str = "UNPROVEN"  # "PROVEN", "UNPROVEN", "TEST_DOUBLE"

    @abstractmethod
    def execute(
        self,
        task: Dict[str, Any],
        workspace_path: str,
        allowed_scope: Dict[str, Any],
        base_sha: str,
        timeout_seconds: int = 3600,
    ) -> WorkerExecutionResult:
        """Execute a task in an isolated workspace."""
        pass
