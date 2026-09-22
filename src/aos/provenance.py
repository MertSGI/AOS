"""Authoritative exact SHA provenance validator for AOS candidates and runtimes.

INVARIANT:
A full Git commit SHA must originate from an authoritative Git object or GitHub object.
Never synthesize the missing suffix of a SHA from a prefix.

Required identity invariant for a CI-proven candidate:
LOCAL_GIT_HEAD
=
BUILD_SOURCE_SHA
=
CANDIDATE_MANIFEST_SOURCE_SHA
=
RUNTIME_SOURCE_SHA
=
GITHUB_ACTIONS_HEAD_SHA

Literal full-string equality required across all 40 hex characters.
Fails closed if any value differs, is malformed, or missing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from aos.process_utils import run_headless

SHA_REGEX = re.compile(r"^[0-9a-fA-F]{40}$")


class ProvenanceError(ValueError):
    """Raised when SHA provenance verification fails."""


@dataclass(frozen=True)
class ProvenanceValidationResult:
    valid: bool
    status: str  # e.g. "PROVEN", "UNPROVEN", "FAIL"
    errors: Sequence[str]
    local_git_head: Optional[str] = None
    build_source_sha: Optional[str] = None
    candidate_manifest_source_sha: Optional[str] = None
    runtime_source_sha: Optional[str] = None
    github_actions_head_sha: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "status": self.status,
            "errors": list(self.errors),
            "local_git_head": self.local_git_head,
            "build_source_sha": self.build_source_sha,
            "candidate_manifest_source_sha": self.candidate_manifest_source_sha,
            "runtime_source_sha": self.runtime_source_sha,
            "github_actions_head_sha": self.github_actions_head_sha,
        }


def is_valid_full_sha(sha: Optional[str]) -> bool:
    """Check if string is an exact 40-character hexadecimal Git SHA."""
    if not isinstance(sha, str):
        return False
    return bool(SHA_REGEX.fullmatch(sha.strip()))


def get_authoritative_git_head(repo_path: Path) -> str:
    """Read authoritative 40-character commit SHA directly from local Git repository using headless execution."""
    resolved_repo = Path(repo_path).resolve()
    proc = run_headless(
        ["git", "-C", str(resolved_repo), "rev-parse", "HEAD"],
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise ProvenanceError(
            f"Failed to query authoritative git HEAD in '{resolved_repo}': {proc.stderr.strip()}"
        )
    raw_sha = proc.stdout.strip().lower()
    if not is_valid_full_sha(raw_sha):
        raise ProvenanceError(
            f"Git rev-parse HEAD returned invalid 40-char SHA: '{raw_sha}' in '{resolved_repo}'"
        )
    return raw_sha


def validate_exact_sha_provenance(
    *,
    local_git_head: Optional[str] = None,
    build_source_sha: Optional[str] = None,
    candidate_manifest_source_sha: Optional[str] = None,
    runtime_source_sha: Optional[str] = None,
    github_actions_head_sha: Optional[str] = None,
    require_ci_sha: bool = False,
    require_local_git_head: bool = True,
) -> ProvenanceValidationResult:
    """Fail-closed validator enforcing literal full-string equality across the provenance chain.

    If any value differs or fails the 40-character hexadecimal requirement,
    validation fails immediately.
    """
    errors: list[str] = []

    fields = {
        "BUILD_SOURCE_SHA": build_source_sha,
        "CANDIDATE_MANIFEST_SOURCE_SHA": candidate_manifest_source_sha,
        "RUNTIME_SOURCE_SHA": runtime_source_sha,
    }
    if require_local_git_head:
        fields = {"LOCAL_GIT_HEAD": local_git_head, **fields}
    if require_ci_sha or github_actions_head_sha is not None:
        fields["GITHUB_ACTIONS_HEAD_SHA"] = github_actions_head_sha

    normalized: dict[str, str] = {}
    for name, val in fields.items():
        if val is None or not str(val).strip():
            errors.append(f"{name} is missing or empty")
            continue
        cleaned = str(val).strip().lower()
        if not is_valid_full_sha(cleaned):
            errors.append(f"{name} is not a valid 40-character hex SHA: '{val}'")
            continue
        normalized[name] = cleaned

    if not errors and normalized:
        # Check literal string equality across all fields
        reference_name = next(iter(normalized))
        reference_val = normalized[reference_name]
        for name, val in normalized.items():
            if val != reference_val:
                errors.append(
                    f"Provenance mismatch: {name} ({val}) != {reference_name} ({reference_val})"
                )

    is_valid = len(errors) == 0 and len(normalized) == len(fields)
    status = "PROVEN" if is_valid else ("UNPROVEN" if not errors and not is_valid else "FAIL")

    return ProvenanceValidationResult(
        valid=is_valid,
        status=status,
        errors=errors,
        local_git_head=normalized.get("LOCAL_GIT_HEAD"),
        build_source_sha=normalized.get("BUILD_SOURCE_SHA"),
        candidate_manifest_source_sha=normalized.get("CANDIDATE_MANIFEST_SOURCE_SHA"),
        runtime_source_sha=normalized.get("RUNTIME_SOURCE_SHA"),
        github_actions_head_sha=normalized.get("GITHUB_ACTIONS_HEAD_SHA"),
    )


def validate_materialized_runtime_provenance(
    *,
    candidate_manifest: Mapping[str, Any],
    build_record: Mapping[str, Any],
    runtime_source_sha: Optional[str],
    runtime_asset_tree_sha256: Optional[str],
) -> ProvenanceValidationResult:
    """Validate an immutable runtime slot without consulting a mutable checkout HEAD.

    Materialization already binds the clean source HEAD and successful CI run to
    the candidate manifest. Runtime validation therefore checks that immutable
    attestation, the separate build record, the running SHA, and the runtime's
    loaded asset-tree digest still agree.
    """
    manifest_source_sha = candidate_manifest.get("candidate_source_sha")
    manifest_build_sha = candidate_manifest.get("build_source_sha")
    build_source_sha = build_record.get("build_source_sha") or build_record.get("source_sha")
    base = validate_exact_sha_provenance(
        build_source_sha=str(build_source_sha) if build_source_sha is not None else None,
        candidate_manifest_source_sha=(
            str(manifest_source_sha) if manifest_source_sha is not None else None
        ),
        runtime_source_sha=runtime_source_sha,
        require_local_git_head=False,
    )
    errors = list(base.errors)

    if str(candidate_manifest.get("provenance") or "").upper() != "PROVEN":
        errors.append("CANDIDATE_MANIFEST_PROVENANCE is not PROVEN")
    try:
        if int(candidate_manifest.get("ci_run_id", 0) or 0) <= 0:
            errors.append("CANDIDATE_MANIFEST_CI_RUN_ID is missing or invalid")
    except (TypeError, ValueError):
        errors.append("CANDIDATE_MANIFEST_CI_RUN_ID is missing or invalid")
    if manifest_build_sha != build_source_sha:
        errors.append(
            "Provenance mismatch: CANDIDATE_MANIFEST_BUILD_SOURCE_SHA "
            f"({manifest_build_sha}) != BUILD_RECORD_SOURCE_SHA ({build_source_sha})"
        )

    manifest_tree = candidate_manifest.get("candidate_tree_sha256")
    tree_pattern = re.compile(r"^[0-9a-fA-F]{64}$")
    if not isinstance(manifest_tree, str) or not tree_pattern.fullmatch(manifest_tree.strip()):
        errors.append("CANDIDATE_MANIFEST_TREE_SHA256 is missing or invalid")
    if not isinstance(runtime_asset_tree_sha256, str) or not tree_pattern.fullmatch(runtime_asset_tree_sha256.strip()):
        errors.append("RUNTIME_ASSET_TREE_SHA256 is missing or invalid")
    elif isinstance(manifest_tree, str) and manifest_tree.strip().lower() != runtime_asset_tree_sha256.strip().lower():
        errors.append(
            "Provenance mismatch: RUNTIME_ASSET_TREE_SHA256 "
            f"({runtime_asset_tree_sha256}) != CANDIDATE_MANIFEST_TREE_SHA256 ({manifest_tree})"
        )

    valid = base.valid and not errors
    return ProvenanceValidationResult(
        valid=valid,
        status="PROVEN" if valid else "FAIL",
        errors=errors,
        build_source_sha=base.build_source_sha,
        candidate_manifest_source_sha=base.candidate_manifest_source_sha,
        runtime_source_sha=base.runtime_source_sha,
    )
