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


def validate_materialized_runtime_provenance(
    *,
    manifest: Mapping[str, Any],
    runtime_source_sha: Optional[str],
    runtime_asset_tree_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate immutable materialized-runtime provenance.

    Three-state semantics are intentional:

    PROVEN
        Every required immutable proof artifact exists and agrees exactly.

    UNPROVEN
        Required proof material is absent. Absence is never silently
        synthesized, aliased, or treated as success.

    FAIL
        Supplied proof material is malformed or contradicts another
        supplied immutable artifact.

    The current development checkout HEAD is deliberately not part of
    live immutable-runtime identity. Materialization already bound the
    candidate to an exact clean source SHA and successful exact-SHA CI.
    A later source checkout movement therefore represents development
    drift, not live deployment corruption.
    """
    if not isinstance(
        manifest,
        Mapping,
    ) or not manifest:

        return {
            "valid":
                False,

            "status":
                "UNPROVEN",

            "errors":
                [
                    "CANDIDATE_MANIFEST_MISSING",
                ],
        }


    missing: list[str] = []
    fatal: list[str] = []


    manifest_source = str(
        manifest.get(
            "candidate_source_sha"
        )
        or ""
    ).strip().lower()


    build_source = str(
        manifest.get(
            "build_source_sha"
        )
        or ""
    ).strip().lower()


    runtime_source = str(
        runtime_source_sha
        or ""
    ).strip().lower()


    manifest_tree = str(
        manifest.get(
            "candidate_tree_sha256"
        )
        or ""
    ).strip().lower()


    runtime_tree = str(
        runtime_asset_tree_sha256
        or ""
    ).strip().lower()


    manifest_provenance = str(
        manifest.get(
            "provenance"
        )
        or ""
    ).strip().upper()


    if not manifest_provenance:
        missing.append(
            "CANDIDATE_MANIFEST_PROVENANCE_MISSING"
        )

    elif (
        manifest_provenance
        !=
        "PROVEN"
    ):
        fatal.append(
            "CANDIDATE_MANIFEST_PROVENANCE_NOT_PROVEN"
        )


    sha_fields = (
        (
            "CANDIDATE_MANIFEST_SOURCE_SHA",
            manifest_source,
        ),
        (
            "BUILD_SOURCE_SHA",
            build_source,
        ),
        (
            "RUNTIME_SOURCE_SHA",
            runtime_source,
        ),
    )


    for (
        name,
        value,
    ) in sha_fields:

        if not value:
            missing.append(
                f"{name}_MISSING"
            )

        elif not is_valid_full_sha(
            value
        ):
            fatal.append(
                f"{name}_INVALID"
            )


    # Only compare SHAs when all three are syntactically valid.
    if (
        is_valid_full_sha(
            manifest_source
        )
        and
        is_valid_full_sha(
            build_source
        )
        and
        is_valid_full_sha(
            runtime_source
        )
        and
        not (
            manifest_source
            ==
            build_source
            ==
            runtime_source
        )
    ):
        fatal.append(
            "MATERIALIZED_RUNTIME_SOURCE_SHA_MISMATCH"
        )


    raw_ci_run_id = manifest.get(
        "ci_run_id"
    )

    if raw_ci_run_id in (
        None,
        "",
    ):
        ci_run_id = 0

        missing.append(
            "CI_RUN_ID_MISSING"
        )

    else:
        try:
            ci_run_id = int(
                raw_ci_run_id
            )

        except (
            TypeError,
            ValueError,
        ):
            ci_run_id = 0

            fatal.append(
                "CI_RUN_ID_INVALID"
            )

        else:
            if ci_run_id <= 0:
                fatal.append(
                    "CI_RUN_ID_INVALID"
                )


    if not manifest_tree:
        missing.append(
            "CANDIDATE_TREE_SHA256_MISSING"
        )

    elif not re.fullmatch(
        r"[0-9a-f]{64}",
        manifest_tree,
    ):
        fatal.append(
            "CANDIDATE_TREE_SHA256_INVALID"
        )


    if not runtime_tree:
        missing.append(
            "RUNTIME_ASSET_TREE_SHA256_MISSING"
        )

    elif not re.fullmatch(
        r"[0-9a-f]{64}",
        runtime_tree,
    ):
        fatal.append(
            "RUNTIME_ASSET_TREE_SHA256_INVALID"
        )


    if (
        re.fullmatch(
            r"[0-9a-f]{64}",
            manifest_tree,
        )
        and
        re.fullmatch(
            r"[0-9a-f]{64}",
            runtime_tree,
        )
        and
        manifest_tree
        !=
        runtime_tree
    ):
        fatal.append(
            "RUNTIME_ASSET_TREE_MISMATCH"
        )


    if fatal:
        status = "FAIL"

    elif missing:
        status = "UNPROVEN"

    else:
        status = "PROVEN"


    return {
        "valid":
            status
            ==
            "PROVEN",

        "status":
            status,

        "errors":
            fatal
            +
            missing,

        "candidate_manifest_source_sha":
            manifest_source
            or
            None,

        "build_source_sha":
            build_source
            or
            None,

        "runtime_source_sha":
            runtime_source
            or
            None,

        "ci_run_id":
            ci_run_id,

        "candidate_tree_sha256":
            manifest_tree
            or
            None,

        "runtime_asset_tree_sha256":
            runtime_tree
            or
            None,
    }

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
) -> ProvenanceValidationResult:
    """Fail-closed validator enforcing literal full-string equality across the provenance chain.

    If any value differs or fails the 40-character hexadecimal requirement,
    validation fails immediately.
    """
    errors: list[str] = []

    fields = {
        "LOCAL_GIT_HEAD": local_git_head,
        "BUILD_SOURCE_SHA": build_source_sha,
        "CANDIDATE_MANIFEST_SOURCE_SHA": candidate_manifest_source_sha,
        "RUNTIME_SOURCE_SHA": runtime_source_sha,
    }
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
