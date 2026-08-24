"""Generic machine-local candidate store for verified controlled execution results."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

CANDIDATE_STORE_CONTRACT_VERSION = "0.1.0"


class CandidateStoreError(Exception):
    """Exception raised when candidate persistence fails."""


def get_default_candidate_store_dir() -> Path:
    """Get the default candidate store base directory."""
    env_dir = os.environ.get("AOS_CANDIDATE_STORE_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    # Default to user-local data directory ~/.aos/candidate_store
    return (Path.home() / ".aos" / "candidate_store").resolve()


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
        target_dir.mkdir(parents=True, exist_ok=False)

        # Copy workspace directory contents
        shutil.copytree(ws_path, target_ws, symlinks=False, ignore_dangling_symlinks=False)

        # Inspect changed paths to build manifest
        paths_manifest: List[Dict[str, Any]] = []
        for rel_path in sorted(changed_paths):
            f_target = target_ws / rel_path
            if f_target.is_symlink():
                raise CandidateStoreError(f"Symlinks are not permitted in candidate workspace: '{rel_path}'")

            if f_target.is_file():
                sz = f_target.stat().st_size
                sha = compute_file_sha256(f_target)
                paths_manifest.append({
                    "path": rel_path,
                    "state": "PRESENT",
                    "size_bytes": sz,
                    "sha256": sha,
                })
            elif not f_target.exists():
                paths_manifest.append({
                    "path": rel_path,
                    "state": "DELETED",
                })
            else:
                raise CandidateStoreError(f"Unsupported filesystem object at '{rel_path}'")

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
