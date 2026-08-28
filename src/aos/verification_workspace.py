"""Generic controller-owned verification workspace abstraction.

Isolates project verification checks from the authoritative worker candidate workspace.
Creates a fresh, verified disposable copy for EACH required check, and verifies that
the copy matches the worker workspace boundary precisely (HEAD, branch, changed_paths,
hashes of present changed files, deleted states, zero Git remotes, zero symlinks).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.candidate_store import _scan_tree_for_symlinks, compute_file_sha256


class VerificationWorkspaceError(Exception):
    """Exception raised when verification workspace isolation or integrity checks fail."""


def verify_copy_zero_remotes(copy_ws: Path) -> None:
    """Verify that the verification copy workspace has zero Git remotes via read-only git inspection."""
    git_dir = copy_ws / ".git"
    if not git_dir.exists():
        return

    try:
        res = subprocess.run(
            ["git", "-C", str(copy_ws), "remote"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            raise VerificationWorkspaceError(
                f"Failed to inspect verification copy git remotes: {res.stderr.strip() or res.stdout.strip()}"
            )
        remotes = res.stdout.strip()
        if remotes:
            raise VerificationWorkspaceError("Verification copy workspace contains git remotes")
    except subprocess.TimeoutExpired as te:
        raise VerificationWorkspaceError(f"Git remote inspection timed out: {te}") from te
    except Exception as e:
        if isinstance(e, VerificationWorkspaceError):
            raise
        raise VerificationWorkspaceError(f"Git remote verification failed: {e}") from e


def inspect_workspace_boundary_state(
    ws_dir: Path,
    runner: Optional[Callable[[List[str], str], subprocess.CompletedProcess]] = None,
) -> Dict[str, Any]:
    """Inspect Git HEAD, branch, changed paths, and file hashes of a workspace."""
    def _run(cmd: List[str]) -> str:
        if runner:
            res = runner(cmd, str(ws_dir))
        else:
            res = subprocess.run(cmd, cwd=str(ws_dir), capture_output=True, text=True)
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise VerificationWorkspaceError(f"Command failed ({' '.join(cmd)}): {err}")
        return res.stdout.strip()

    head = _run(["git", "rev-parse", "HEAD"])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])

    # Query status -z --porcelain -uall and diff -z --name-status
    # We can inspect working tree changed files relative to base commit or status
    status_res = subprocess.run(
        ["git", "status", "-z", "--porcelain", "-uall"],
        cwd=str(ws_dir),
        capture_output=True,
    )
    if status_res.returncode != 0:
        raise VerificationWorkspaceError("Failed to query git status of workspace")

    changed_set = set()
    items = status_res.stdout.split(b"\x00")
    i = 0
    while i < len(items):
        item = items[i]
        if not item:
            i += 1
            continue
        if len(item) >= 3:
            status_code = item[:2].decode("utf-8", errors="replace")
            filepath = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
            if filepath:
                changed_set.add(filepath)
            if any(c in status_code for c in ("R", "C")) and (i + 1) < len(items):
                i += 1
                orig_path = items[i].decode("utf-8", errors="replace").replace("\\", "/")
                if orig_path:
                    changed_set.add(orig_path)
        i += 1

    changed_paths = sorted(list(changed_set))

    # Compute hashes of changed files
    file_states: Dict[str, Dict[str, Any]] = {}
    for p in changed_paths:
        f_p = ws_dir / p
        if f_p.is_symlink():
            raise VerificationWorkspaceError(f"Symlink found in workspace changed path: '{p}'")
        if f_p.is_file():
            file_states[p] = {
                "state": "PRESENT",
                "size_bytes": f_p.stat().st_size,
                "sha256": compute_file_sha256(f_p),
            }
        elif not f_p.exists():
            file_states[p] = {
                "state": "DELETED",
            }
        else:
            raise VerificationWorkspaceError(f"Unsupported filesystem object at changed path '{p}'")

    return {
        "head": head,
        "branch": branch,
        "changed_paths": changed_paths,
        "file_states": file_states,
    }


class VerificationWorkspaceCopy:
    """Disposable isolated copy of a worker candidate workspace for running exactly ONE verification check."""

    def __init__(self, original_ws_path: Path, check_id: str):
        self.original_ws_path = original_ws_path.resolve()
        self.check_id = check_id
        self.copy_dir: Optional[Path] = None

    def __enter__(self) -> Path:
        self.create_and_verify()
        assert self.copy_dir is not None
        return self.copy_dir

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def create_and_verify(self) -> Path:
        """Create a fresh copy of original_ws_path and verify boundary integrity."""
        if not self.original_ws_path.is_dir():
            raise VerificationWorkspaceError(
                f"Original workspace does not exist or is not a directory: {self.original_ws_path}"
            )

        # 1. Pre-copy scan on original workspace: zero symlinks
        try:
            _scan_tree_for_symlinks(self.original_ws_path, label="original worker workspace")
        except Exception as e:
            raise VerificationWorkspaceError(f"Symlinks are not permitted in original worker workspace: {e}") from e

        # 2. Inspect original boundary state
        orig_state = inspect_workspace_boundary_state(self.original_ws_path)

        # 3. Create fresh temp directory for verification copy
        temp_dir = Path(tempfile.mkdtemp(prefix=f"aos_verify_{self.check_id.lower().replace(':', '_')}_")).resolve()
        self.copy_dir = temp_dir

        try:
            # 4. Copy workspace directory contents without dereferencing symlinks
            shutil.copytree(self.original_ws_path, temp_dir, symlinks=True, ignore_dangling_symlinks=False, dirs_exist_ok=True)


            # 5. Post-copy scan on verification copy: zero symlinks
            _scan_tree_for_symlinks(temp_dir, label="verification copy workspace")

            # 6. Verify zero Git remotes
            verify_copy_zero_remotes(temp_dir)

            # 7. Inspect copy boundary state and compare with original
            copy_state = inspect_workspace_boundary_state(temp_dir)

            if copy_state["head"] != orig_state["head"]:
                raise VerificationWorkspaceError(
                    f"Verification copy HEAD mismatch: copy '{copy_state['head']}' != original '{orig_state['head']}'"
                )
            if copy_state["branch"] != orig_state["branch"]:
                raise VerificationWorkspaceError(
                    f"Verification copy branch mismatch: copy '{copy_state['branch']}' != original '{orig_state['branch']}'"
                )
            if copy_state["changed_paths"] != orig_state["changed_paths"]:
                raise VerificationWorkspaceError(
                    f"Verification copy changed_paths mismatch: copy {copy_state['changed_paths']} != original {orig_state['changed_paths']}"
                )
            if copy_state["file_states"] != orig_state["file_states"]:
                raise VerificationWorkspaceError(
                    "Verification copy file states/hashes do not match original worker workspace"
                )

            return temp_dir
        except Exception as e:
            self.cleanup()
            if isinstance(e, VerificationWorkspaceError):
                raise
            raise VerificationWorkspaceError(f"Failed to create and verify verification copy: {e}") from e

    def cleanup(self) -> None:
        """Safely remove disposable verification copy."""
        if self.copy_dir and self.copy_dir.exists():
            def _remove_readonly(func, path, excinfo):
                import stat
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception:
                    pass

            shutil.rmtree(self.copy_dir, onerror=_remove_readonly)
            if self.copy_dir.exists():
                shutil.rmtree(self.copy_dir, ignore_errors=True)
            self.copy_dir = None

