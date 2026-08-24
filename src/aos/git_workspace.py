"""Deterministic isolated git workspace abstraction for AOS-3."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional


def normalize_github_repository_name(url_or_repo: str) -> str:
    """Normalize supported GitHub repository URLs or 'owner/repo' strings to 'owner/repo'.

    Fails closed if the host is not github.com or if the format is invalid.
    """
    s = url_or_repo.strip()
    if not s:
        raise ValueError("Repository identifier cannot be empty")

    # If already clean owner/repo
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", s):
        if s.endswith(".git"):
            s = s[:-4]
        return s

    # Match supported GitHub remote URLs strictly
    patterns = [
        r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        r"^ssh://(?:git@)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        r"^git://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    ]

    for p in patterns:
        m = re.match(p, s)
        if m:
            owner, repo = m.group(1), m.group(2)
            return f"{owner}/{repo}"

    raise ValueError(f"Origin '{url_or_repo}' is not a valid supported GitHub repository format")


def inspect_github_repository_identity(
    local_repo_path: str,
    runner: Optional[Callable[[List[str], str], subprocess.CompletedProcess]] = None,
) -> str:
    """Dedicated read-only inspector to determine GitHub repository identity of a local git repo."""
    if not local_repo_path or not isinstance(local_repo_path, str) or not os.path.exists(local_repo_path):
        raise RuntimeError(f"Local repository path '{local_repo_path}' does not exist")

    def _run(cmd: List[str]) -> str:
        if runner:
            res = runner(cmd, local_repo_path)
        else:
            res = subprocess.run(cmd, cwd=local_repo_path, capture_output=True, text=True)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"Command failed ({' '.join(cmd)}): {err}")
        return res.stdout.strip()

    # Query origin remote
    url = ""
    try:
        url = _run(["git", "remote", "get-url", "origin"])
    except Exception:
        try:
            url = _run(["git", "config", "--get", "remote.origin.url"])
        except Exception as e:
            raise RuntimeError(f"Unresolved or missing origin remote in '{local_repo_path}': {e}") from e

    if not url:
        raise RuntimeError(f"Origin remote in '{local_repo_path}' returned empty URL")

    return normalize_github_repository_name(url)


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
            return inspect_github_repository_identity(self.repository_path, runner=self.runner)
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
            # 1. Clone without checkout and without hardlinks into temp directory
            self._run_git(["clone", "--no-checkout", "--no-hardlinks", self.repository_path, temp_root], cwd=self.repository_path)

            # 2. REMOVE ALL REMOTES inside disposable clone
            remotes = self._run_git(["remote"], cwd=temp_root).splitlines()
            for r in remotes:
                r_name = r.strip()
                if r_name:
                    self._run_git(["remote", "remove", r_name], cwd=temp_root)

            # Explicitly verify no remotes remain
            remaining_remotes = self._run_git(["remote"], cwd=temp_root).strip()
            if remaining_remotes:
                raise RuntimeError(f"Failed to remove all remotes: remaining: '{remaining_remotes}'")

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
        """Get list of normalized repository-relative file paths changed since base_sha (NUL-delimited).

        Fails closed by raising RuntimeError if diff or status cannot be queried.
        Preserves leading/trailing whitespaces within filenames without calling strip().
        Preserves BOTH source and destination paths for renames and copies.
        """
        if not self.workspace_dir:
            raise RuntimeError("Workspace not initialized")
        base = from_sha or self.base_sha
        changed = set()

        # 1. NUL-delimited committed diff with --name-status since base
        diff_bytes = self._run_raw_git(["diff", "-z", "--name-status", base, "HEAD"])
        items = diff_bytes.split(b"\x00")
        i = 0
        while i < len(items):
            item = items[i]
            if not item:
                i += 1
                continue
            status = item.decode("utf-8", errors="replace")
            if status.startswith(("R", "C")):
                # Rename or copy record: status, source, dest
                if (i + 2) < len(items):
                    src_p = items[i + 1].decode("utf-8", errors="replace").replace("\\", "/")
                    dst_p = items[i + 2].decode("utf-8", errors="replace").replace("\\", "/")
                    if src_p:
                        changed.add(src_p)
                    if dst_p:
                        changed.add(dst_p)
                    i += 3
                else:
                    i += 1
            else:
                # Other statuses (M, A, D, etc.): status, path
                if (i + 1) < len(items):
                    p = items[i + 1].decode("utf-8", errors="replace").replace("\\", "/")
                    if p:
                        changed.add(p)
                    i += 2
                else:
                    i += 1

        # 2. NUL-delimited status for working tree & untracked files
        status_bytes = self._run_raw_git(["status", "-z", "--porcelain", "-uall"])
        items = status_bytes.split(b"\x00")
        i = 0
        while i < len(items):
            item = items[i]
            if not item:
                i += 1
                continue
            if len(item) >= 3:
                status_code = item[:2].decode("utf-8", errors="replace")
                # Destination / main path (do not .strip()!)
                filepath = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
                if filepath:
                    changed.add(filepath)
                # If rename or copy, next item is the source path
                if any(c in status_code for c in ("R", "C")) and (i + 1) < len(items):
                    i += 1
                    orig_path = items[i].decode("utf-8", errors="replace").replace("\\", "/")
                    if orig_path:
                        changed.add(orig_path)
            i += 1

        # Validate path traversal or absolute path artifacts without strip()
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
        res = self.runner(["git"] + args, target_cwd)
        if res.returncode != 0:
            err = (res.stderr or "").strip() if isinstance(res.stderr, str) else (res.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Git query failed ('git {' '.join(args)}'): {err}")
        if isinstance(res.stdout, bytes):
            return res.stdout
        return (res.stdout or "").encode("utf-8")

    def cleanup(self) -> None:
        """Safely clean up disposable worker clone directory."""
        if self.workspace_dir and os.path.exists(self.workspace_dir):
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
            self.workspace_dir = None
