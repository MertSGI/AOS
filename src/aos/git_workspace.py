"""Deterministic isolated git workspace abstraction for AOS-3."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional


def enforce_aos_branch_namespace(task_id: str, requested_branch: Optional[str] = None) -> str:
    """Enforce that controlled execution worker branches strictly remain within the 'aos/' namespace."""
    clean_task_id = task_id.lower().replace("_", "-")
    if not requested_branch:
        return f"aos/{clean_task_id}"

    branch = requested_branch.strip()
    if branch.startswith("aos/"):
        return branch

    # Strip illegal prefixes or path escape attempts
    parts = [p for p in branch.replace("\\", "/").split("/") if p and p not in (".", "..")]
    leaf_name = parts[-1] if parts else clean_task_id
    return f"aos/{leaf_name}"


class GitWorkspace:
    """Isolated Git workspace created from an exact base commit SHA."""

    def __init__(
        self,
        repository_path: str,
        base_sha: str,
        task_id: str,
        requested_branch: Optional[str] = None,
        runner: Optional[Callable[[List[str], str], subprocess.CompletedProcess]] = None,
    ):
        self.repository_path = repository_path
        self.base_sha = base_sha
        self.task_id = task_id
        self.worker_branch = enforce_aos_branch_namespace(task_id, requested_branch)
        self.runner = runner or self._default_runner

        self.workspace_dir: Optional[str] = None
        self.initial_head_sha: Optional[str] = None

    def _default_runner(self, cmd: List[str], cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    def _run_git(self, args: List[str], cwd: Optional[str] = None) -> str:
        target_cwd = cwd or self.workspace_dir or self.repository_path
        res = self.runner(["git"] + args, target_cwd)
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"Git command failed ('git {' '.join(args)}'): {err_msg}")
        return res.stdout.strip()

    def setup(self) -> str:
        """Create isolated worktree or clone from base_sha in a dedicated temp directory."""
        if not self.base_sha or len(self.base_sha) != 40:
            raise ValueError(f"Invalid base_sha: '{self.base_sha}'")

        # Verify base_sha commit exists in source repository
        try:
            self._run_git(["cat-file", "-e", f"{self.base_sha}^{{commit}}"], cwd=self.repository_path)
        except Exception as e:
            raise RuntimeError(f"Base commit SHA '{self.base_sha}' not found in repository '{self.repository_path}': {e}") from e

        temp_root = tempfile.mkdtemp(prefix=f"aos_worktree_{self.task_id.lower()}_")
        self.workspace_dir = temp_root

        # Create isolated worktree or clone
        try:
            self._run_git(["worktree", "add", "-b", self.worker_branch, temp_root, self.base_sha], cwd=self.repository_path)
        except Exception:
            # Fallback if worktree fails (e.g. branch already exists or bare repo): try clone
            if os.path.exists(temp_root):
                shutil.rmtree(temp_root, ignore_errors=True)
            self._run_git(["clone", "--no-checkout", self.repository_path, temp_root], cwd=self.repository_path)
            self.workspace_dir = temp_root
            self._run_git(["checkout", "-b", self.worker_branch, self.base_sha], cwd=temp_root)

        self.initial_head_sha = self.get_current_head()
        return self.workspace_dir

    def get_current_head(self) -> str:
        """Get exact 40-character commit SHA of current workspace HEAD."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        return self._run_git(["rev-parse", "HEAD"])

    def get_status(self) -> List[str]:
        """Get list of modified/untracked file statuses."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        out = self._run_git(["status", "--porcelain"])
        return [line.strip() for line in out.splitlines() if line.strip()]

    def get_changed_files(self, from_sha: Optional[str] = None) -> List[str]:
        """Get list of file paths changed since base_sha (staged, unstaged, and committed)."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        base = from_sha or self.base_sha
        changed = set()

        # 1. Committed diff since base
        diff_name = self._run_git(["diff", "--name-only", base, "HEAD"])
        for p in diff_name.splitlines():
            if p.strip():
                changed.add(p.strip().replace("\\", "/"))

        # 2. Uncommitted/working tree diff against HEAD
        status_lines = self.get_status()
        for line in status_lines:
            if len(line) >= 3:
                filepath = line[3:].strip().replace("\\", "/")
                # Handle renamed files "old -> new"
                if " -> " in filepath:
                    filepath = filepath.split(" -> ")[1].strip()
                if filepath:
                    changed.add(filepath)

        return sorted(list(changed))

    def get_diff(self, from_sha: Optional[str] = None) -> str:
        """Get unified git diff since base_sha including working tree changes."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        base = from_sha or self.base_sha
        diff_committed = self._run_git(["diff", base, "HEAD"])
        diff_uncommitted = self._run_git(["diff", "HEAD"])
        parts = [d for d in (diff_committed, diff_uncommitted) if d.strip()]
        return "\n".join(parts)

    def cleanup(self) -> None:
        """Safely clean up isolated worktree and temp directory."""
        if self.workspace_dir and os.path.exists(self.workspace_dir):
            try:
                self._run_git(["worktree", "remove", "--force", self.workspace_dir], cwd=self.repository_path)
            except Exception:
                pass
            if os.path.exists(self.workspace_dir):
                shutil.rmtree(self.workspace_dir, ignore_errors=True)
            self.workspace_dir = None
