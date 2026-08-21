"""Deterministic isolated git workspace abstraction for AOS-3."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional, Tuple


def normalize_github_repository_name(url_or_repo: str) -> str:
    """Normalize common HTTPS, SSH, or shorthand GitHub repository strings to 'owner/repo' format."""
    s = url_or_repo.strip()
    if s.endswith(".git"):
        s = s[:-4]
    if "github.com/" in s:
        s = s.split("github.com/")[1]
    elif "github.com:" in s:
        s = s.split("github.com:")[1]
    parts = [p for p in s.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return s


def enforce_aos_branch_namespace(task_id: str, requested_branch: Optional[str] = None) -> str:
    """Enforce that controlled execution worker branches strictly remain within the 'aos/' namespace."""
    clean_task_id = task_id.lower().replace("_", "-")
    return f"aos/{clean_task_id}"


class GitWorkspace:
    """Isolated Git workspace created via a disposable clone from an exact base commit SHA."""

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

    def get_origin_repository_name(self) -> Optional[str]:
        """Query target repository's origin remote and normalize to owner/repo."""
        try:
            url = self._run_git(["remote", "get-url", "origin"], cwd=self.repository_path)
            return normalize_github_repository_name(url)
        except Exception:
            try:
                url = self._run_git(["config", "--get", "remote.origin.url"], cwd=self.repository_path)
                return normalize_github_repository_name(url)
            except Exception:
                return None

    def setup(self) -> str:
        """Create disposable clone from base_sha, disable remotes, and checkout worker branch."""
        if not self.base_sha or len(self.base_sha) != 40:
            raise ValueError(f"Invalid base_sha: '{self.base_sha}'")

        # Verify base_sha commit exists in source repository
        try:
            self._run_git(["cat-file", "-e", f"{self.base_sha}^{{commit}}"], cwd=self.repository_path)
        except Exception as e:
            raise RuntimeError(f"Base commit SHA '{self.base_sha}' not found in repository '{self.repository_path}': {e}") from e

        temp_root = tempfile.mkdtemp(prefix=f"aos_disposable_clone_{self.task_id.lower()}_")
        self.workspace_dir = temp_root

        try:
            # 1. Clone without checkout into temp directory
            self._run_git(["clone", "--no-checkout", self.repository_path, temp_root], cwd=self.repository_path)

            # 2. REMOVE/DISABLE ALL REMOTES inside disposable clone
            remotes = self._run_git(["remote"], cwd=temp_root).splitlines()
            for r in remotes:
                r_name = r.strip()
                if r_name:
                    try:
                        self._run_git(["remote", "remove", r_name], cwd=temp_root)
                    except Exception:
                        pass

            # 3. Checkout worker branch from exact base_sha
            self._run_git(["checkout", "-b", self.worker_branch, self.base_sha], cwd=temp_root)

        except Exception as e:
            if os.path.exists(temp_root):
                shutil.rmtree(temp_root, ignore_errors=True)
            self.workspace_dir = None
            raise RuntimeError(f"Failed to setup disposable clone workspace: {e}") from e

        self.initial_head_sha = self.get_current_head()
        if self.initial_head_sha != self.base_sha:
            self.cleanup()
            raise RuntimeError(f"Initial HEAD SHA '{self.initial_head_sha}' != expected base SHA '{self.base_sha}'")

        return self.workspace_dir

    def get_current_head(self) -> str:
        """Get exact 40-character commit SHA of current workspace HEAD."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        return self._run_git(["rev-parse", "HEAD"])

    def get_current_branch(self) -> str:
        """Get current branch name of workspace."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        return self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])

    def get_status(self) -> List[str]:
        """Get list of modified/untracked file statuses."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        out = self._run_git(["status", "--porcelain"])
        return [line.strip() for line in out.splitlines() if line.strip()]

    def get_changed_files(self, from_sha: Optional[str] = None) -> List[str]:
        """Get list of normalized repository-relative file paths changed since base_sha (NUL-delimited)."""
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        base = from_sha or self.base_sha
        changed = set()

        # 1. NUL-delimited committed diff since base
        try:
            diff_bytes = self._run_raw_git(["diff", "-z", "--name-only", base, "HEAD"])
            for p in diff_bytes.split(b"\x00"):
                clean = p.decode("utf-8", errors="replace").strip().replace("\\", "/")
                if clean:
                    changed.add(clean)
        except Exception:
            pass

        # 2. NUL-delimited status for working tree & untracked files
        try:
            status_bytes = self._run_raw_git(["status", "-z", "--porcelain"])
            # Format: 'XY path\x00' or 'R  path1\x00path2\x00'
            items = status_bytes.split(b"\x00")
            i = 0
            while i < len(items):
                item = items[i]
                if not item:
                    i += 1
                    continue
                if len(item) >= 3:
                    status_code = item[:2].decode("utf-8", errors="replace")
                    filepath = item[3:].decode("utf-8", errors="replace").strip().replace("\\", "/")
                    if "R" in status_code and (i + 1) < len(items):
                        # Renamed file has next NUL item as new path
                        i += 1
                        filepath = items[i].decode("utf-8", errors="replace").strip().replace("\\", "/")
                    if filepath:
                        changed.add(filepath)
                i += 1
        except Exception:
            pass

        # Validate path traversal or absolute path artifacts
        result = []
        for path in sorted(list(changed)):
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"Path traversal or absolute path violation detected: '{path}'")
            # Normalize path stripping leading './'
            if path.startswith("./"):
                path = path[2:]
            result.append(path)

        return result

    def _run_raw_git(self, args: List[str]) -> bytes:
        target_cwd = self.workspace_dir or self.repository_path
        res = subprocess.run(["git"] + args, cwd=target_cwd, capture_output=True)
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"Raw git command failed: {err}")
        return res.stdout

    def cleanup(self) -> None:
        """Safely clean up disposable worker clone directory."""
        if self.workspace_dir and os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
            self.workspace_dir = None
