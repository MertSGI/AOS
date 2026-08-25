"""Generic machine-local candidate store for verified controlled execution results and failed candidate quarantine."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

CANDIDATE_STORE_CONTRACT_VERSION = "0.1.0"
QUARANTINE_STORE_CONTRACT_VERSION = "0.1.0"


class CandidateStoreError(Exception):
    """Exception raised when candidate persistence fails."""


def get_default_candidate_store_dir() -> Path:
    """Get the default candidate store base directory."""
    env_dir = os.environ.get("AOS_CANDIDATE_STORE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Default to user-local data directory ~/.aos/candidate_store
    return (Path.home() / ".aos" / "candidate_store").resolve()


def get_default_quarantine_store_dir() -> Path:
    """Get the default quarantine store base directory."""
    env_dir = os.environ.get("AOS_QUARANTINE_STORE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Default to candidate store dir parent or ~/.aos/quarantine_store
    base_cand = get_default_candidate_store_dir()
    return (base_cand.parent / "quarantine_store").resolve()


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA256 hex digest of a regular file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def canonical_json_bytes(data: Dict[str, Any]) -> bytes:
    """Produce deterministic canonical UTF-8 JSON bytes."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _scan_tree_for_symlinks(root: Path, label: str = "workspace") -> None:
    """Recursively scan a directory tree using safe non-following traversal to ensure zero symlinks exist."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dir_p = Path(dirpath)
        # Check if current directory entry itself is a symlink
        if dir_p.is_symlink():
            raise CandidateStoreError(f"Symlinks are not permitted in {label}: directory symlink found at '{dir_p.relative_to(root)}'")

        for d in dirnames:
            p = dir_p / d
            if p.is_symlink():
                raise CandidateStoreError(f"Symlinks are not permitted in {label}: directory symlink found at '{p.relative_to(root)}'")

        for f in filenames:
            p = dir_p / f
            if p.is_symlink():
                raise CandidateStoreError(f"Symlinks are not permitted in {label}: file symlink found at '{p.relative_to(root)}'")


def _verify_candidate_zero_remotes(target_ws: Path) -> None:
    """Verify that the persisted candidate workspace has zero Git remotes via read-only git inspection."""
    git_dir = target_ws / ".git"
    if not git_dir.exists():
        return

    try:
        res = subprocess.run(
            ["git", "-C", str(target_ws), "remote"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            raise CandidateStoreError(f"Failed to inspect candidate workspace git remotes: {res.stderr.strip() or res.stdout.strip()}")
        remotes = res.stdout.strip()
        if remotes:
            raise CandidateStoreError("Candidate workspace contains git remotes after persistence")
    except subprocess.TimeoutExpired as te:
        raise CandidateStoreError(f"Git remote inspection timed out: {te}") from te
    except Exception as e:
        if isinstance(e, CandidateStoreError):
            raise
        raise CandidateStoreError(f"Git remote verification failed: {e}") from e


def _build_paths_manifest(target_ws: Path, changed_paths: List[str]) -> List[Dict[str, Any]]:
    """Validate relative changed paths and construct deterministic paths manifest."""
    paths_manifest: List[Dict[str, Any]] = []
    for raw_path in sorted(changed_paths):
        if not raw_path or not isinstance(raw_path, str):
            raise CandidateStoreError(f"Invalid changed path: empty or non-string value '{raw_path}'")

        # Reject drive-qualified or absolute paths
        p_obj = Path(raw_path)
        if p_obj.is_absolute() or p_obj.drive or raw_path.startswith("/") or raw_path.startswith("\\"):
            raise CandidateStoreError(f"Changed path must be repository-relative and not absolute: '{raw_path}'")

        # Reject parent traversal components
        parts = p_obj.parts
        if ".." in parts or "." in parts:
            raise CandidateStoreError(f"Changed path contains traversal components: '{raw_path}'")

        # Normalized relative path
        rel_path = p_obj.as_posix()

        # Ensure resolution does not escape target_ws
        f_target = (target_ws / p_obj).resolve()
        try:
            f_target.relative_to(target_ws.resolve())
        except ValueError:
            raise CandidateStoreError(f"Changed path escapes candidate workspace: '{raw_path}'")

        target_direct = target_ws / rel_path
        if target_direct.is_symlink():
            raise CandidateStoreError(f"Symlinks are not permitted in candidate workspace: '{rel_path}'")

        if target_direct.is_file():
            sz = target_direct.stat().st_size
            sha = compute_file_sha256(target_direct)
            paths_manifest.append({
                "path": rel_path,
                "state": "PRESENT",
                "size_bytes": sz,
                "sha256": sha,
            })
        elif not target_direct.exists():
            paths_manifest.append({
                "path": rel_path,
                "state": "DELETED",
            })
        else:
            raise CandidateStoreError(f"Unsupported filesystem object at '{rel_path}'")
    return paths_manifest


def persist_verified_candidate(
    workspace_path: str,
    project_id: str,
    task_id: str,
    gate: str,
    control_source_sha: str,
    execution_base_sha: str,
    worker_branch: str,
    initial_head_sha: str,
    final_head_sha: str,
    changed_paths: List[str],
    source_repo_path: Optional[str] = None,
    candidate_store_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist verified candidate workspace to machine-local candidate store.

    Returns:
        Metadata dictionary with candidate_id, manifest_sha256, changed_paths, etc.
    """
    ws_path = Path(workspace_path).resolve()
    if not ws_path.is_dir():
        raise CandidateStoreError(f"Workspace path does not exist or is not a directory: {ws_path}")

    # Determine candidate store base directory
    if candidate_store_dir:
        base_store = Path(candidate_store_dir).expanduser().resolve()
    else:
        base_store = get_default_candidate_store_dir()

    # Safety checks: ensure candidate store is not inside source repository
    if source_repo_path:
        src_repo = Path(source_repo_path).resolve()
        try:
            base_store.relative_to(src_repo)
            raise CandidateStoreError(f"Candidate store directory cannot reside inside source repository: {base_store}")
        except ValueError:
            pass  # Not inside source repository

    # Ensure candidate store is not inside the disposable workspace
    try:
        base_store.relative_to(ws_path)
        raise CandidateStoreError(f"Candidate store directory cannot reside inside disposable workspace: {base_store}")
    except ValueError:
        pass

    try:
        base_store.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise CandidateStoreError(f"Cannot create or access candidate store directory '{base_store}': {e}") from e

    candidate_id = f"cand_{uuid.uuid4().hex[:16]}"
    target_dir = base_store / candidate_id

    if target_dir.exists():
        raise CandidateStoreError(f"Candidate directory collision: destination '{target_dir}' already exists")

    target_ws = target_dir / "workspace"

    try:
        # Pre-copy scan: Ensure source workspace contains zero symlinks anywhere
        _scan_tree_for_symlinks(ws_path, label="source workspace")

        target_dir.mkdir(parents=True, exist_ok=False)

        # Copy workspace directory contents without dereferencing symlinks
        shutil.copytree(ws_path, target_ws, symlinks=True, ignore_dangling_symlinks=False)

        # Post-copy scan: Ensure persisted candidate workspace contains zero symlinks
        _scan_tree_for_symlinks(target_ws, label="persisted candidate workspace")

        # Verify candidate git repository has zero remotes
        _verify_candidate_zero_remotes(target_ws)

        # Validate and inspect changed paths to build manifest
        paths_manifest = _build_paths_manifest(target_ws, changed_paths)

        now_iso = datetime.now(timezone.utc).isoformat()
        manifest_data = {
            "schema_version": "0.1.0",
            "candidate_store_contract_version": CANDIDATE_STORE_CONTRACT_VERSION,
            "candidate_id": candidate_id,
            "project_id": project_id,
            "task_id": task_id,
            "gate": gate,
            "control_source_sha": control_source_sha,
            "execution_base_sha": execution_base_sha,
            "worker_branch": worker_branch,
            "initial_head_sha": initial_head_sha,
            "final_head_sha": final_head_sha,
            "changed_paths": paths_manifest,
            "created_at": now_iso,
        }

        # Calculate manifest_sha256
        canon_bytes = canonical_json_bytes(manifest_data)
        manifest_sha256 = hashlib.sha256(canon_bytes).hexdigest()

        # Write manifest.json
        manifest_file = target_dir / "manifest.json"
        manifest_file.write_bytes(canon_bytes)

        return {
            "status": "PERSISTED",
            "candidate_store_contract_version": CANDIDATE_STORE_CONTRACT_VERSION,
            "candidate_id": candidate_id,
            "manifest_sha256": manifest_sha256,
            "changed_paths": changed_paths,
            "execution_base_sha": execution_base_sha,
            "control_source_sha": control_source_sha,
        }
    except Exception as e:
        # Partial failure cleanup
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        if isinstance(e, CandidateStoreError):
            raise
        raise CandidateStoreError(f"Failed to persist candidate: {e}") from e


def persist_quarantine_candidate(
    workspace_path: str,
    project_id: str,
    task_id: str,
    gate: str,
    control_source_sha: str,
    execution_base_sha: str,
    worker_branch: str,
    initial_head_sha: str,
    final_head_sha: str,
    worker_changed_paths: List[str],
    source_repo_path: Optional[str] = None,
    quarantine_store_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist unverified scope-clean failed candidate workspace to machine-local quarantine store.

    This snapshot is UNTRUSTED and exists strictly for forensic diagnosis/review.
    It does NOT satisfy candidate_persistence, cannot authorize promotion, and returns
    status='QUARANTINED_UNVERIFIED'.
    """
    ws_path = Path(workspace_path).resolve()
    if not ws_path.is_dir():
        raise CandidateStoreError(f"Workspace path does not exist or is not a directory: {ws_path}")

    # Determine quarantine store base directory
    if quarantine_store_dir:
        base_store = Path(quarantine_store_dir).expanduser().resolve()
    else:
        base_store = get_default_quarantine_store_dir()

    # Safety checks: ensure quarantine store is not inside source repository
    if source_repo_path:
        src_repo = Path(source_repo_path).resolve()
        try:
            base_store.relative_to(src_repo)
            raise CandidateStoreError(f"Quarantine store directory cannot reside inside source repository: {base_store}")
        except ValueError:
            pass  # Not inside source repository

    # Ensure quarantine store is not inside the disposable workspace
    try:
        base_store.relative_to(ws_path)
        raise CandidateStoreError(f"Quarantine store directory cannot reside inside disposable workspace: {base_store}")
    except ValueError:
        pass

    try:
        base_store.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise CandidateStoreError(f"Cannot create or access quarantine store directory '{base_store}': {e}") from e

    quarantine_id = f"quar_{uuid.uuid4().hex[:16]}"
    target_dir = base_store / quarantine_id

    if target_dir.exists():
        raise CandidateStoreError(f"Quarantine directory collision: destination '{target_dir}' already exists")

    target_ws = target_dir / "workspace"

    try:
        # Pre-copy scan: Ensure source workspace contains zero symlinks anywhere
        _scan_tree_for_symlinks(ws_path, label="source workspace for quarantine")

        target_dir.mkdir(parents=True, exist_ok=False)

        # Copy workspace directory contents without dereferencing symlinks
        shutil.copytree(ws_path, target_ws, symlinks=True, ignore_dangling_symlinks=False)

        # Post-copy scan: Ensure persisted quarantine workspace contains zero symlinks
        _scan_tree_for_symlinks(target_ws, label="quarantined candidate workspace")

        # Verify quarantine git repository has zero remotes
        _verify_candidate_zero_remotes(target_ws)

        # Defense-in-depth: If target_ws is a Git repository, verify all currently changed paths match worker_changed_paths exactly
        if (target_ws / ".git").exists():
            try:
                status_res = subprocess.run(
                    ["git", "status", "-z", "--porcelain", "-uall"],
                    cwd=str(target_ws),
                    capture_output=True,
                    check=True,
                )
                raw_items = status_res.stdout.split(b"\x00")
                current_target_changed = set()
                idx = 0
                while idx < len(raw_items):
                    item = raw_items[idx]
                    if not item:
                        idx += 1
                        continue
                    if len(item) >= 3:
                        status_code = item[:2].decode("utf-8", errors="replace")
                        filepath = item[3:].decode("utf-8", errors="replace").replace("\\", "/")
                        if filepath:
                            current_target_changed.add(filepath)
                        if any(c in status_code for c in ("R", "C")) and (idx + 1) < len(raw_items):
                            idx += 1
                            orig_path = raw_items[idx].decode("utf-8", errors="replace").replace("\\", "/")
                            if orig_path:
                                current_target_changed.add(orig_path)
                    idx += 1

                if sorted(list(current_target_changed)) != sorted(worker_changed_paths):
                    raise CandidateStoreError(
                        f"Quarantine changed paths mismatch: workspace has {sorted(list(current_target_changed))} but manifest was given {sorted(worker_changed_paths)}"
                    )
            except Exception as e:
                if isinstance(e, CandidateStoreError):
                    raise
                raise CandidateStoreError(f"Failed to verify quarantine workspace changed paths completeness: {e}") from e


        # Validate and inspect changed paths to build manifest
        paths_manifest = _build_paths_manifest(target_ws, worker_changed_paths)

        now_iso = datetime.now(timezone.utc).isoformat()

        manifest_data = {
            "schema_version": "0.1.0",
            "quarantine_store_contract_version": QUARANTINE_STORE_CONTRACT_VERSION,
            "quarantine_id": quarantine_id,
            "project_id": project_id,
            "task_id": task_id,
            "gate": gate,
            "control_source_sha": control_source_sha,
            "execution_base_sha": execution_base_sha,
            "worker_branch": worker_branch,
            "initial_head_sha": initial_head_sha,
            "final_head_sha": final_head_sha,
            "changed_paths": paths_manifest,
            "created_at": now_iso,
        }

        # Calculate manifest_sha256
        canon_bytes = canonical_json_bytes(manifest_data)
        manifest_sha256 = hashlib.sha256(canon_bytes).hexdigest()

        # Write manifest.json
        manifest_file = target_dir / "manifest.json"
        manifest_file.write_bytes(canon_bytes)

        return {
            "status": "QUARANTINED_UNVERIFIED",
            "quarantine_store_contract_version": QUARANTINE_STORE_CONTRACT_VERSION,
            "quarantine_id": quarantine_id,
            "manifest_sha256": manifest_sha256,
            "worker_changed_paths": worker_changed_paths,
            "execution_base_sha": execution_base_sha,
            "control_source_sha": control_source_sha,
        }
    except Exception as e:
        # Partial failure cleanup
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        if isinstance(e, CandidateStoreError):
            raise
        raise CandidateStoreError(f"Failed to persist quarantine candidate: {e}") from e
