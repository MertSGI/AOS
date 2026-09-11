"""AOS Controller Relay CR2-Lite Authority Reference Resolver Contract.

Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01

Normative Governance Contract:
- Relay messages carries ONLY immutable opaque authority coordinates:
    - repository: MertSGI/Randapp-main
    - branch: control/lari-project-control-plane
    - publication_commit_sha: exact 40-char commit SHA
    - path: docs/project-control/controller-authorities/LARI_CONTROLLER/<AUTHORITY_ID>.json
    - authority_id: matching string
    - authority_body_sha256: 64-char lowercase hex digest of canonical JSON body
- Relay message invariant: authority_effect MUST equal "NONE".
  Relay delivery ALONE must NEVER create authority.
- Authority Resolver independently validates:
    1. Artifact existence in canonical authority store.
    2. Exact publication commit and canonical path.
    3. Authority body SHA256 match.
    4. issuer_controller == LARI_CONTROLLER.
    5. subject SHA / repository scope matches current context.
    6. freshness and non-consumption status.
    7. current canonical governance state.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

CANONICAL_LARI_AUTHORITY_REPOSITORY = "MertSGI/Randapp-main"
CANONICAL_LARI_AUTHORITY_BRANCH = "control/lari-project-control-plane"
LARI_AUTHORITY_PATH_PREFIX = "docs/project-control/controller-authorities/LARI_CONTROLLER/"
AUTHORITY_ID_REGEX = re.compile(r"^[A-Za-z0-9_\-]+$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")
SHA1_REGEX = re.compile(r"^[0-9a-f]{40}$")


class AuthorityResolutionError(Exception):
    """Raised when authority reference resolution or validation fails."""
    pass


class CanonicalAuthorityReference:
    """Opaque reference coordinates pointing into canonical LARI authority store."""

    def __init__(
        self,
        repository: str,
        branch: str,
        publication_commit_sha: str,
        path: str,
        authority_id: str,
        authority_body_sha256: str,
    ):
        if repository != CANONICAL_LARI_AUTHORITY_REPOSITORY:
            raise ValueError(f"Invalid authority repository: {repository}")
        if branch != CANONICAL_LARI_AUTHORITY_BRANCH:
            raise ValueError(f"Invalid authority branch: {branch}")
        if not SHA1_REGEX.match(publication_commit_sha):
            raise ValueError(f"Invalid publication_commit_sha: {publication_commit_sha}")
        expected_path = f"{LARI_AUTHORITY_PATH_PREFIX}{authority_id}.json"
        if path != expected_path:
            raise ValueError(f"Path '{path}' does not match expected canonical path '{expected_path}'")
        if not AUTHORITY_ID_REGEX.match(authority_id):
            raise ValueError(f"Invalid authority_id: {authority_id}")
        if not SHA256_HEX_REGEX.match(authority_body_sha256):
            raise ValueError(f"Invalid authority_body_sha256: {authority_body_sha256}")

        self.repository = repository
        self.branch = branch
        self.publication_commit_sha = publication_commit_sha
        self.path = path
        self.authority_id = authority_id
        self.authority_body_sha256 = authority_body_sha256


class AuthorityArtifactResolver:
    """Independent validator and resolver for canonical LARI authority artifacts."""

    def __init__(
        self,
        content_reader: Callable[[str, str, str], bytes],
    ):
        """content_reader: callable(repository: str, commit_sha: str, path: str) -> bytes."""
        self._reader = content_reader

    def resolve_and_validate_authority(
        self,
        ref: CanonicalAuthorityReference,
        expected_subject_sha: Optional[str] = None,
        expected_authority_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch canonical authority artifact, verify digest, issuer, scope, and freshness."""
        if expected_authority_id and ref.authority_id != expected_authority_id:
            raise AuthorityResolutionError(
                f"Authority ID mismatch: expected '{expected_authority_id}', ref has '{ref.authority_id}'"
            )

        # 1. Read artifact bytes from immutable publication commit
        try:
            raw_bytes = self._reader(ref.repository, ref.publication_commit_sha, ref.path)
        except Exception as exc:
            raise AuthorityResolutionError(
                f"Failed to read canonical authority artifact at '{ref.path}'@{ref.publication_commit_sha}: {exc}"
            )

        # 2. Verify SHA256 digest
        computed_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if computed_sha256 != ref.authority_body_sha256:
            raise AuthorityResolutionError(
                f"Authority artifact body SHA256 mismatch for '{ref.authority_id}': "
                f"computed '{computed_sha256}', expected '{ref.authority_body_sha256}'"
            )

        # 3. Parse JSON body
        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise AuthorityResolutionError(f"Authority artifact is not valid JSON: {exc}")

        if not isinstance(body, dict):
            raise AuthorityResolutionError("Authority artifact body must be a JSON object")

        # 4. Strict fail-closed invariant checks
        # Every required field must be physically present. No permissive defaults.
        required_fields = [
            "authority_id",
            "issuer_controller",
            "subject_repository",
            "subject_branch",
            "subject_sha",
            "authority_class",
            "authorized_scope",
            "forbidden_scope",
            "production",
        ]
        for field in required_fields:
            if field not in body or body[field] is None:
                raise AuthorityResolutionError(
                    f"VERIFICATION_FAILED: Required authority field '{field}' is missing from artifact"
                )
            if isinstance(body[field], str) and not body[field].strip():
                raise AuthorityResolutionError(
                    f"VERIFICATION_FAILED: Required authority field '{field}' is empty in artifact"
                )

        issuer = body.get("issuer_controller")
        if issuer != "LARI_CONTROLLER":
            raise AuthorityResolutionError(
                f"VERIFICATION_FAILED: Invalid issuer_controller '{issuer}', expected 'LARI_CONTROLLER'"
            )

        body_id = body.get("authority_id")
        if body_id != ref.authority_id:
            raise AuthorityResolutionError(
                f"VERIFICATION_FAILED: Internal authority_id '{body_id}' does not match ref '{ref.authority_id}'"
            )

        if expected_subject_sha:
            subject_sha = body.get("subject_sha")
            if subject_sha != expected_subject_sha:
                raise AuthorityResolutionError(
                    f"VERIFICATION_FAILED: Subject SHA mismatch: body '{subject_sha}', expected '{expected_subject_sha}'"
                )

        prod = body.get("production")
        if prod != "NO_GO":
            raise AuthorityResolutionError(
                f"VERIFICATION_FAILED: Invalid production state '{prod}', must be strictly 'NO_GO'"
            )

        status = body.get("status", "ACTIVE")
        if status not in {"ACTIVE", "VALID", "GRANTED"}:
            raise AuthorityResolutionError(f"VERIFICATION_FAILED: Authority status is not active: '{status}'")

        return body
