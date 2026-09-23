"""Content-bound identity helpers for completed workspace reads."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Optional


def normalize_read_path(path: str) -> str:
    normalized = str(PurePosixPath(path.replace("\\", "/")))
    if not normalized or normalized == "." or normalized.startswith("/"):
        raise ValueError("read path must be workspace-relative")
    if any(part in ("", ".", "..") for part in PurePosixPath(normalized).parts):
        raise ValueError("read path must be normalized and confined")
    return normalized.casefold()


def build_workspace_source_generation(
    *,
    project_id: str,
    canonical_source_sha: str,
    canonical_execution_base_sha: Optional[str],
) -> str:
    payload = {
        "schema_version": "1.0.0",
        "project_id": str(project_id),
        "canonical_source_sha": str(canonical_source_sha),
        "canonical_execution_base_sha": str(canonical_execution_base_sha or "NONE"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_read_identity(
    *,
    normalized_path: str,
    content_sha256: str,
    workspace_source_generation: str,
) -> str:
    path = normalize_read_path(normalized_path)
    for name, value in (
        ("content_sha256", content_sha256),
        ("workspace_source_generation", workspace_source_generation),
    ):
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError(f"{name} must be a SHA-256 hex digest")
    record = (
        f"{len(path.encode('utf-8'))}:{path}|"
        f"64:{content_sha256.lower()}|64:{workspace_source_generation.lower()}"
    ).encode("utf-8")
    return hashlib.sha256(record).hexdigest()
