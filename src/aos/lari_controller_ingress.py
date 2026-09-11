"""LARI Controller ChatGPT/MCP Ingress Surface around ControllerRelayService.

Implementation Authority ID: LARI-AOS-AUTONOMOUS-QUALITY-LOOP-AND-RELAY-INGRESS-BOOTSTRAP-20260910-01
Hardening Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

This module provides a strictly bounded, authenticated MCP and ChatGPT connector surface
for the LARI_CONTROLLER principal.

Allowed operations:
1. relay_get_head() -> Dict[str, Any]
2. relay_get_latest_unconsumed() -> Dict[str, Any]
3. relay_read_message(message_id: str) -> Dict[str, Any]
4. relay_publish_message(raw_json_str: str, expected_head: str) -> Dict[str, Any]
5. relay_publish_receipt(raw_json_str: str, expected_head: str) -> Dict[str, Any]
6. authority_fetch_and_verify(authority_id: str, exact_commit_sha: Optional[str] = None, expected_subject_sha: Optional[str] = None) -> Dict[str, Any]
7. publish_controller_authority(authority_id: str, content_json: str, expected_control_plane_head: str) -> Dict[str, Any]

Security Invariants:
- Principal binding is IMMUTABLE and hardcoded to LARI_CONTROLLER for this ingress surface.
- Callers (AOS_CONTROLLER / AG / external) CANNOT pass or override the principal parameter.
- Mutation uses exclusively the dedicated GitHub App installation identity via GitDataCASRelayTransport.
- Authority publication is strictly bounded to repository MertSGI/Randapp-main, branch control/lari-project-control-plane,
  under path docs/project-control/controller-authorities/LARI_CONTROLLER/<AUTHORITY_ID>.json.
- Authority artifact format is STRICT CANONICAL JSON (.json).
- No generic Git operations. No arbitrary path writes. No product branch writes.
- Inbound discovery strictly delegates to canonical get_latest_unconsumed_inbound detector.
- RELAY_MESSAGE != AUTHORITY, RELAY_AUTHORITY_EFFECT=NONE.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import hmac
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.controller_relay import (
    MESSAGE_ID_REGEX,
    ControllerRelayError,
    ControllerRelayValidationResult,
    _parse_raw_relay_bytes_strict,
    compute_message_content_sha256,
    validate_controller_relay_message_raw,
    validate_controller_relay_receipt_raw,
)
from aos.controller_relay_authority_resolver import (
    AuthorityArtifactResolver,
    AuthorityResolutionError,
    CanonicalAuthorityReference,
)
from aos.controller_relay_detector import (
    UnconsumedInboundMessage,
    get_latest_unconsumed_inbound,
)
from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REPOSITORY,
    ControllerRelayTransportError,
    GitDataCASRelayTransport,
    GitHubRequester,
)
from aos.controller_relay_service import (
    ControllerPrincipal,
    ControllerRelayService,
    derive_message_path,
    derive_receipt_path,
)

# Immutable Principal Identity for this ingress
LARI_CONTROLLER_PRINCIPAL = ControllerPrincipal("LARI_CONTROLLER")

# Bounded Authority Publication Constants
AUTHORITY_STORE_REPOSITORY: str = "MertSGI/Randapp-main"
AUTHORITY_STORE_BRANCH: str = "control/lari-project-control-plane"
AUTHORITY_STORE_PATH_PREFIX: str = "docs/project-control/controller-authorities/LARI_CONTROLLER/"
AUTHORITY_ID_REGEX = re.compile(r"^[A-Za-z0-9_\-]+$")
GIT_SHA_REGEX = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")


class LariControllerIngressError(Exception):
    """Base exception for LARI Controller Ingress failures."""
    pass


class TransportSessionAuth:
    """Authenticated ingress boundary session credentials.

    Enforces that transport/session identity is established before dispatch.
    A plain data object supplied by the caller is not proof of authentication.
    Only an injected trusted authenticator produces valid TransportSessionAuth
    with is_authenticated=True bound to principal_role=LARI_CONTROLLER.
    """

    def __init__(
        self,
        session_token: str,
        principal_role: str,
        is_authenticated: bool = True,
    ):
        self.session_token = session_token
        self.principal_role = principal_role
        self.is_authenticated = is_authenticated

    def is_valid_lari_controller(self) -> bool:
        return (
            self.is_authenticated is True
            and self.principal_role == "LARI_CONTROLLER"
            and isinstance(self.session_token, str)
            and len(self.session_token) >= 32
        )


def create_secret_session_authenticator(expected_secret: str) -> Callable[[str], Optional[TransportSessionAuth]]:
    """Factory for constant-time secret-backed session authenticator."""
    if not isinstance(expected_secret, str) or len(expected_secret) < 32:
        raise ValueError("STARTUP_FAIL_CLOSED: Ingress expected_secret must be a string of at least 32 characters")

    def authenticator(token: str) -> Optional[TransportSessionAuth]:
        if not isinstance(token, str) or len(token) < 32:
            return None
        if hmac.compare_digest(token, expected_secret):
            return TransportSessionAuth(
                session_token=token,
                principal_role="LARI_CONTROLLER",
                is_authenticated=True,
            )
        return None

    return authenticator


def _json_no_duplicates_loader(raw_bytes: bytes) -> Dict[str, Any]:
    """Strict JSON parser rejecting duplicate keys and non-dict JSON root."""
    def pairs_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"Duplicate key detected in JSON: '{k}'")
            d[k] = v
        return d

    text = raw_bytes.decode("utf-8")
    parsed = json.loads(text, object_pairs_hook=pairs_hook)
    if not isinstance(parsed, dict):
        raise ValueError("Root JSON payload must be an object/dict")
    return parsed


class LariControllerIngressService:
    """Narrowly scoped, authenticated LARI_CONTROLLER connector surface."""

    def __init__(
        self,
        relay_service: ControllerRelayService,
        authority_requester: Optional[GitHubRequester] = None,
        session_authenticator: Optional[Callable[[str], Optional[TransportSessionAuth]]] = None,
    ):
        """Initialize with an active ControllerRelayService, optional authority requester, and session authenticator."""
        self._relay_service = relay_service
        self._principal = LARI_CONTROLLER_PRINCIPAL
        self._authority_requester = authority_requester
        self._session_authenticator = session_authenticator

    @property
    def principal_controller_id(self) -> str:
        """Authoritative immutable caller identity."""
        return self._principal.controller_id

    def authenticate_session(self, session_token: Optional[str]) -> bool:
        """Verify transport/session identity before tool dispatch.

        FAIL CLOSED:
        - If session_authenticator is absent/None: UNAUTHORIZED (returns False).
        - If session_token is None/empty: UNAUTHORIZED (returns False).
        - If session_authenticator raises or returns None: UNAUTHORIZED (returns False).
        - If authenticated context is not LARI_CONTROLLER: UNAUTHORIZED (returns False).
        - Caller-supplied token length alone MUST NEVER establish identity.
        """
        if self._session_authenticator is None:
            return False
        if not session_token or not isinstance(session_token, str) or len(session_token) < 32:
            return False
        try:
            auth = self._session_authenticator(session_token)
        except Exception:
            return False
        if auth is None:
            return False
        return bool(auth.is_valid_lari_controller())

    # --------------------------------------------------------------------------
    # 1. relay_get_head
    # --------------------------------------------------------------------------
    def relay_get_head(self) -> Dict[str, Any]:
        """Get current HEAD commit SHA of the canonical control/controller-relay branch."""
        try:
            head_sha = self._relay_service.get_head()
            return {
                "status": "SUCCESS",
                "repository": FIXED_RELAY_REPOSITORY,
                "branch": FIXED_RELAY_BRANCH,
                "head_sha": head_sha,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }

    # --------------------------------------------------------------------------
    # 2. relay_get_latest_unconsumed
    # --------------------------------------------------------------------------
    def relay_get_latest_unconsumed(
        self,
        for_controller: Optional[str] = "LARI_CONTROLLER",
    ) -> Dict[str, Any]:
        """Find the latest message sent to LARI_CONTROLLER using canonical CR2-lite detector.

        Delegates strictly to get_latest_unconsumed_inbound:
        - complete immutable history validation
        - directed sequence validation
        - supersession handling
        - thread/in_reply_to binding
        - receipt lifecycle validation
        - requires_reply integrity check
        - actual Git publication ordinal ordering (never filename lexical order)
        """
        target_recipient = self._principal.controller_id
        if for_controller and for_controller != target_recipient:
            raise ValueError(
                f"Ingress principal violation: cannot query unconsumed messages for '{for_controller}' (bound to '{target_recipient}')"
            )

        try:
            head_sha = self._relay_service.get_head()
            latest_unconsumed: Optional[UnconsumedInboundMessage] = get_latest_unconsumed_inbound(
                service=self._relay_service,
                target_controller=target_recipient,
                expected_head=head_sha,
            )

            if latest_unconsumed is None:
                return {
                    "status": "EMPTY",
                    "head_sha": head_sha,
                    "target_controller": target_recipient,
                    "unconsumed_count": 0,
                    "latest_unconsumed": None,
                }

            return {
                "status": "SUCCESS",
                "head_sha": head_sha,
                "target_controller": target_recipient,
                "unconsumed_count": 1,
                "latest_unconsumed": latest_unconsumed.message,
                "publication_commit_sha": latest_unconsumed.publication_commit_sha,
                "path": latest_unconsumed.path,
                "publication_ordinal": latest_unconsumed.publication_ordinal,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }

    # --------------------------------------------------------------------------
    # 3. relay_read_message
    # --------------------------------------------------------------------------
    def relay_read_message(self, message_id: str) -> Dict[str, Any]:
        """Read a message and its full receipt history by deterministic message_id."""
        if not isinstance(message_id, str) or not MESSAGE_ID_REGEX.match(message_id):
            return {
                "status": "ERROR",
                "error": f"Invalid message_id format: '{message_id}'",
            }

        try:
            head_sha = self._relay_service.get_head()
            msg_provenances = self._relay_service.list_message_provenances(ref=head_sha)
            receipt_provenances = self._relay_service.list_receipt_provenances(ref=head_sha)

            target_msg = None
            target_pub_sha = None
            for msg, pub_sha in msg_provenances:
                if msg.get("message_id") == message_id:
                    target_msg = msg
                    target_pub_sha = pub_sha
                    break

            if not target_msg:
                return {
                    "status": "NOT_FOUND",
                    "message_id": message_id,
                    "head_sha": head_sha,
                }

            msg_receipts = [
                rcpt for rcpt, _ in receipt_provenances
                if rcpt.get("message_id") == message_id
            ]

            return {
                "status": "SUCCESS",
                "message_id": message_id,
                "message": target_msg,
                "publication_commit_sha": target_pub_sha,
                "receipts": msg_receipts,
                "head_sha": head_sha,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }

    # --------------------------------------------------------------------------
    # 4. relay_publish_message
    # --------------------------------------------------------------------------
    def relay_publish_message(
        self,
        raw_json_str: str,
        expected_head: str,
    ) -> Dict[str, Any]:
        """Publish a Relay message on behalf of LARI_CONTROLLER.

        Principal is forced to LARI_CONTROLLER. Caller payload 'from' field MUST equal 'LARI_CONTROLLER'.
        """
        if not isinstance(raw_json_str, str) or not raw_json_str.strip():
            return {"status": "ERROR", "error": "raw_json_str must be a non-empty string"}

        if not isinstance(expected_head, str) or not GIT_SHA_REGEX.match(expected_head):
            return {"status": "ERROR", "error": f"Invalid expected_head SHA: '{expected_head}'"}

        raw_bytes = raw_json_str.encode("utf-8")

        # Publish through service with immutable LARI_CONTROLLER principal
        res = self._relay_service.publish_message(
            raw_bytes=raw_bytes,
            expected_head=expected_head,
            principal=self._principal,
        )

        if not res.is_valid or res.disposition != "PASS":
            return {
                "status": "FAILED",
                "disposition": res.disposition,
                "errors": res.errors,
            }

        return {
            "status": "SUCCESS",
            "disposition": res.disposition,
            "commit_sha": res.details.get("commit_sha") or res.details.get("PUBLISHED_COMMIT_SHA"),
            "path": res.details.get("path") or res.details.get("PUBLISHED_PATH"),
        }

    # --------------------------------------------------------------------------
    # 5. relay_publish_receipt
    # --------------------------------------------------------------------------
    def relay_publish_receipt(
        self,
        raw_json_str: str,
        expected_head: str,
    ) -> Dict[str, Any]:
        """Publish a Relay receipt on behalf of LARI_CONTROLLER.

        Principal is forced to LARI_CONTROLLER. Caller payload 'actor' field MUST equal 'LARI_CONTROLLER'.
        """
        if not isinstance(raw_json_str, str) or not raw_json_str.strip():
            return {"status": "ERROR", "error": "raw_json_str must be a non-empty string"}

        if not isinstance(expected_head, str) or not GIT_SHA_REGEX.match(expected_head):
            return {"status": "ERROR", "error": f"Invalid expected_head SHA: '{expected_head}'"}

        raw_bytes = raw_json_str.encode("utf-8")

        # Publish through service with immutable LARI_CONTROLLER principal
        res = self._relay_service.publish_receipt(
            raw_bytes=raw_bytes,
            expected_head=expected_head,
            principal=self._principal,
        )

        if not res.is_valid or res.disposition != "PASS":
            return {
                "status": "FAILED",
                "disposition": res.disposition,
                "errors": res.errors,
            }

        return {
            "status": "SUCCESS",
            "disposition": res.disposition,
            "commit_sha": res.details.get("commit_sha") or res.details.get("PUBLISHED_COMMIT_SHA"),
            "path": res.details.get("path") or res.details.get("PUBLISHED_PATH"),
        }

    # --------------------------------------------------------------------------
    # 6. authority_fetch_and_verify (Delegates to canonical AuthorityArtifactResolver)
    # --------------------------------------------------------------------------
    def authority_fetch_and_verify(
        self,
        authority_id: str,
        exact_commit_sha: Optional[str] = None,
        expected_subject_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch and independently verify canonical JSON authority artifact via AuthorityArtifactResolver.

        Delegates to canonical AuthorityArtifactResolver and CanonicalAuthorityReference.
        """
        if not isinstance(authority_id, str) or not AUTHORITY_ID_REGEX.match(authority_id):
            return {"status": "ERROR", "error": f"Invalid authority_id format: '{authority_id}'"}

        rel_path = f"{AUTHORITY_STORE_PATH_PREFIX}{authority_id}.json"

        if not self._authority_requester:
            return {
                "status": "ERROR",
                "error": "Authority requester not configured for authority verification",
            }

        try:
            # 1. Resolve commit SHA if not given
            target_sha = exact_commit_sha
            if not target_sha:
                ref_path = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/ref/heads/{AUTHORITY_STORE_BRANCH}"
                st, body, _ = self._authority_requester.request("GET", ref_path)
                if st != 200:
                    return {"status": "ERROR", "error": f"Failed to fetch control branch ref (HTTP {st})"}
                data = json.loads(body.decode("utf-8"))
                target_sha = data.get("object", {}).get("sha")

            if not target_sha or not GIT_SHA_REGEX.match(target_sha):
                return {"status": "ERROR", "error": f"Could not resolve valid target SHA: {target_sha}"}

            # Helper content reader for AuthorityArtifactResolver
            def content_reader(repo: str, commit_sha: str, path: str) -> bytes:
                # 1. Fetch commit to find tree
                commit_url = f"/repos/{repo}/git/commits/{commit_sha}"
                st_c, body_c, _ = self._authority_requester.request("GET", commit_url)
                if st_c != 200:
                    raise FileNotFoundError(f"Commit not found: {commit_sha}")
                tree_sha = json.loads(body_c.decode("utf-8"))["tree"]["sha"]

                # 2. Fetch tree recursive
                tree_url = f"/repos/{repo}/git/trees/{tree_sha}?recursive=1"
                st_t, body_t, _ = self._authority_requester.request("GET", tree_url)
                if st_t != 200:
                    raise FileNotFoundError(f"Tree not found: {tree_sha}")
                tree_data = json.loads(body_t.decode("utf-8"))

                blob_sha = None
                for item in tree_data.get("tree", []):
                    if item.get("path") == path and item.get("type") == "blob":
                        blob_sha = item.get("sha")
                        break

                if not blob_sha:
                    raise FileNotFoundError(f"Artifact path not found: {path} at commit {commit_sha}")

                # 3. Fetch blob
                blob_url = f"/repos/{repo}/git/blobs/{blob_sha}"
                st_b, body_b, _ = self._authority_requester.request("GET", blob_url)
                if st_b != 200:
                    raise FileNotFoundError(f"Blob not found: {blob_sha}")
                blob_data = json.loads(body_b.decode("utf-8"))
                encoding = blob_data.get("encoding")
                content_str = blob_data.get("content", "")

                if encoding == "base64":
                    return base64.b64decode(content_str.replace("\n", "").replace("\r", ""))
                return content_str.encode("utf-8")

            # 2. Fetch artifact raw bytes to compute digest for reference
            try:
                raw_bytes = content_reader(AUTHORITY_STORE_REPOSITORY, target_sha, rel_path)
            except FileNotFoundError:
                return {
                    "status": "NOT_FOUND",
                    "authority_id": authority_id,
                    "target_path": rel_path,
                    "commit_sha": target_sha,
                }

            computed_sha256 = hashlib.sha256(raw_bytes).hexdigest()

            # 3. Construct CanonicalAuthorityReference and resolve via canonical AuthorityArtifactResolver
            ref = CanonicalAuthorityReference(
                repository=AUTHORITY_STORE_REPOSITORY,
                branch=AUTHORITY_STORE_BRANCH,
                publication_commit_sha=target_sha,
                path=rel_path,
                authority_id=authority_id,
                authority_body_sha256=computed_sha256,
            )

            resolver = AuthorityArtifactResolver(content_reader=content_reader)
            verified_body = resolver.resolve_and_validate_authority(
                ref=ref,
                expected_subject_sha=expected_subject_sha,
                expected_authority_id=authority_id,
            )

            return {
                "status": "SUCCESS",
                "authority_id": authority_id,
                "issuer_controller": verified_body.get("issuer_controller"),
                "publication_commit_sha": target_sha,
                "canonical_path": rel_path,
                "authority_body_sha256": computed_sha256,
                "subject_sha": verified_body.get("subject_sha"),
                "status_field": verified_body.get("status"),
                "body": verified_body,
            }
        except AuthorityResolutionError as are:
            return {
                "status": "VERIFICATION_FAILED",
                "error": str(are),
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }

    # --------------------------------------------------------------------------
    # 7. publish_controller_authority (Strict Canonical JSON, CAS-Bound)
    # --------------------------------------------------------------------------
    def publish_controller_authority(
        self,
        authority_id: str,
        content_json: str,
        expected_control_plane_head: str,
    ) -> Dict[str, Any]:
        """Publish an immutable Controller authority JSON document to canonical control plane.

        Destination repository: MertSGI/Randapp-main
        Destination branch: control/lari-project-control-plane
        Destination path: docs/project-control/controller-authorities/LARI_CONTROLLER/<AUTHORITY_ID>.json

        Enforces:
        - Valid canonical JSON, rejects malformed JSON or duplicate keys
        - Body must be a JSON object/dict
        - Body authority_id == authority_id parameter
        - Body issuer_controller == 'LARI_CONTROLLER'
        - Computes canonical SHA-256 digest over stored bytes
        - CAS check against expected_control_plane_head
        """
        if not isinstance(authority_id, str) or not AUTHORITY_ID_REGEX.match(authority_id):
            return {"status": "ERROR", "error": f"Invalid authority_id format: '{authority_id}'"}

        if not isinstance(content_json, str) or not content_json.strip():
            return {"status": "ERROR", "error": "content_json must be a non-empty string"}

        # 1. Parse JSON strictly rejecting duplicate keys and non-dict root
        try:
            body_dict = _json_no_duplicates_loader(content_json.encode("utf-8"))
        except Exception as exc:
            return {
                "status": "REJECTED",
                "error": f"Malformed or non-strict JSON authority payload: {exc}",
            }

        # 2. Enforce required authority body fields
        body_auth_id = body_dict.get("authority_id")
        if body_auth_id != authority_id:
            return {
                "status": "REJECTED",
                "error": f"Authority payload authority_id ('{body_auth_id}') does not match requested id ('{authority_id}')",
            }

        issuer = body_dict.get("issuer_controller")
        if issuer != "LARI_CONTROLLER":
            return {
                "status": "REJECTED",
                "error": f"Invalid issuer_controller '{issuer}', must be 'LARI_CONTROLLER'",
            }

        status = body_dict.get("status", "ACTIVE")
        if status not in {"ACTIVE", "VALID", "GRANTED"}:
            return {
                "status": "REJECTED",
                "error": f"Invalid authority status '{status}', must be one of ACTIVE, VALID, GRANTED",
            }

        if not isinstance(expected_control_plane_head, str) or not GIT_SHA_REGEX.match(expected_control_plane_head):
            return {"status": "ERROR", "error": f"Invalid expected_control_plane_head SHA: '{expected_control_plane_head}'"}

        if not self._authority_requester:
            return {"status": "ERROR", "error": "Authority requester not configured for authority publication"}

        target_path = f"{AUTHORITY_STORE_PATH_PREFIX}{authority_id}.json"

        # 3. Canonical UTF-8 serialization and SHA256 computation
        canonical_bytes = json.dumps(body_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        authority_body_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

        try:
            # 1. Verify current ref head matches expected_control_plane_head (CAS)
            ref_path = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/ref/heads/{AUTHORITY_STORE_BRANCH}"
            st, body, _ = self._authority_requester.request("GET", ref_path)
            if st != 200:
                return {"status": "ERROR", "error": f"Failed to fetch current control plane head (HTTP {st})"}
            ref_data = json.loads(body.decode("utf-8"))
            current_head = ref_data.get("object", {}).get("sha")

            if current_head != expected_control_plane_head:
                return {
                    "status": "HOLD_CAS_RACE",
                    "error": f"Current control plane head '{current_head}' != expected '{expected_control_plane_head}'",
                }

            # 2. Get base tree from current head commit
            commit_path = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/commits/{current_head}"
            st, body, _ = self._authority_requester.request("GET", commit_path)
            if st != 200:
                return {"status": "ERROR", "error": f"Failed to fetch commit '{current_head}' (HTTP {st})"}
            base_tree_sha = json.loads(body.decode("utf-8"))["tree"]["sha"]

            # 3. Create blob for authority JSON
            b64_content = base64.b64encode(canonical_bytes).decode("ascii")
            blob_req_body = json.dumps({"content": b64_content, "encoding": "base64"}).encode("utf-8")
            st, body, _ = self._authority_requester.request("POST", f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/blobs", body=blob_req_body)
            if st != 201:
                return {"status": "ERROR", "error": f"Failed to create blob for authority document (HTTP {st})"}
            new_blob_sha = json.loads(body.decode("utf-8"))["sha"]

            # 4. Create new tree (append-only target path)
            tree_req_body = json.dumps({
                "base_tree": base_tree_sha,
                "tree": [{
                    "path": target_path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": new_blob_sha,
                }],
            }).encode("utf-8")
            st, body, _ = self._authority_requester.request("POST", f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/trees", body=tree_req_body)
            if st != 201:
                return {"status": "ERROR", "error": f"Failed to create tree for authority document (HTTP {st})"}
            new_tree_sha = json.loads(body.decode("utf-8"))["sha"]

            # 5. Create new commit
            commit_msg = f"authority(LARI_CONTROLLER): publish {authority_id}"
            commit_req_body = json.dumps({
                "message": commit_msg,
                "tree": new_tree_sha,
                "parents": [current_head],
            }).encode("utf-8")
            st, body, _ = self._authority_requester.request("POST", f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/commits", body=commit_req_body)
            if st != 201:
                return {"status": "ERROR", "error": f"Failed to create commit for authority document (HTTP {st})"}
            new_commit_sha = json.loads(body.decode("utf-8"))["sha"]

            # 6. CAS patch branch ref (non-force)
            patch_req_body = json.dumps({
                "sha": new_commit_sha,
                "force": False,
            }).encode("utf-8")
            st, body, _ = self._authority_requester.request(
                "PATCH",
                f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/refs/heads/{AUTHORITY_STORE_BRANCH}",
                body=patch_req_body,
            )
            if st != 200:
                return {"status": "HOLD_CAS_RACE", "error": f"CAS branch update failed (HTTP {st}): {body.decode('utf-8')}"}

            return {
                "status": "SUCCESS",
                "authority_id": authority_id,
                "path": target_path,
                "publication_commit_sha": new_commit_sha,
                "authority_body_sha256": authority_body_sha256,
                "parent_sha": current_head,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }


# --------------------------------------------------------------------------
# MCP TOOL REGISTRY & DISPATCHER FOR CHATGPT CONNECTOR
# --------------------------------------------------------------------------

LARI_INGRESS_TOOL_DEFINITIONS = [
    {
        "name": "relay_get_head",
        "description": "Fetch current HEAD commit SHA of the canonical control/controller-relay branch.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "relay_get_latest_unconsumed",
        "description": "Retrieve the latest unconsumed Relay message addressed to LARI_CONTROLLER using canonical CR2-lite detection.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "relay_read_message",
        "description": "Read a Relay message and its receipt audit trail by deterministic message_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The deterministic message ID (e.g. CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001)"},
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "relay_publish_message",
        "description": "Publish an outbound Relay message from LARI_CONTROLLER to AOS_CONTROLLER.",
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_json_str": {"type": "string", "description": "Canonical UTF-8 JSON serialized message payload string"},
                "expected_head": {"type": "string", "description": "Exact 40-hex commit SHA of current relay branch HEAD for CAS protection"},
            },
            "required": ["raw_json_str", "expected_head"],
        },
    },
    {
        "name": "relay_publish_receipt",
        "description": "Publish a receipt (OBSERVED, VERIFIED, ACKNOWLEDGED, CONSUMED) for a message from LARI_CONTROLLER.",
        "input_schema": {
            "type": "object",
            "properties": {
                "raw_json_str": {"type": "string", "description": "Canonical UTF-8 JSON serialized receipt payload string"},
                "expected_head": {"type": "string", "description": "Exact 40-hex commit SHA of current relay branch HEAD for CAS protection"},
            },
            "required": ["raw_json_str", "expected_head"],
        },
    },
    {
        "name": "authority_fetch_and_verify",
        "description": "Fetch and verify an immutable Controller authority document from the project control plane via AuthorityArtifactResolver.",
        "input_schema": {
            "type": "object",
            "properties": {
                "authority_id": {"type": "string", "description": "Identifier of the controller authority"},
                "exact_commit_sha": {"type": "string", "description": "Optional exact 40-hex commit SHA to pin verification"},
                "expected_subject_sha": {"type": "string", "description": "Optional expected subject SHA to validate scope"},
            },
            "required": ["authority_id"],
        },
    },
    {
        "name": "publish_controller_authority",
        "description": "Publish an immutable Controller authority JSON document to the project control plane repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "authority_id": {"type": "string", "description": "Identifier of the controller authority"},
                "content_json": {"type": "string", "description": "Strict canonical JSON string of the authority artifact"},
                "expected_control_plane_head": {"type": "string", "description": "Exact 40-hex commit SHA of control/lari-project-control-plane HEAD"},
            },
            "required": ["authority_id", "content_json", "expected_control_plane_head"],
        },
    },
]


def dispatch_lari_ingress_call(
    service: LariControllerIngressService,
    tool_name: str,
    arguments: Dict[str, Any],
    session_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch an inbound tool call from ChatGPT / MCP to LariControllerIngressService.

    Enforces transport/session authentication and tool allowlist boundaries.
    """
    # Verify transport/session authentication boundary (fail closed)
    if not session_token or not service.authenticate_session(session_token):
        return {
            "status": "UNAUTHORIZED",
            "error": "Authentication failed: invalid, missing, or unauthorized session credential for LARI_CONTROLLER",
        }

    if tool_name == "relay_get_head":
        return service.relay_get_head()
    elif tool_name == "relay_get_latest_unconsumed":
        return service.relay_get_latest_unconsumed()
    elif tool_name == "relay_read_message":
        return service.relay_read_message(arguments.get("message_id", ""))
    elif tool_name == "relay_publish_message":
        return service.relay_publish_message(
            raw_json_str=arguments.get("raw_json_str", ""),
            expected_head=arguments.get("expected_head", ""),
        )
    elif tool_name == "relay_publish_receipt":
        return service.relay_publish_receipt(
            raw_json_str=arguments.get("raw_json_str", ""),
            expected_head=arguments.get("expected_head", ""),
        )
    elif tool_name == "authority_fetch_and_verify":
        return service.authority_fetch_and_verify(
            authority_id=arguments.get("authority_id", ""),
            exact_commit_sha=arguments.get("exact_commit_sha"),
            expected_subject_sha=arguments.get("expected_subject_sha"),
        )
    elif tool_name == "publish_controller_authority":
        return service.publish_controller_authority(
            authority_id=arguments.get("authority_id", ""),
            content_json=arguments.get("content_json", arguments.get("content_markdown", "")),
            expected_control_plane_head=arguments.get("expected_control_plane_head", ""),
        )
    else:
        return {
            "status": "ERROR",
            "error": f"Unauthorized tool operation '{tool_name}'. Allowed operations: {[t['name'] for t in LARI_INGRESS_TOOL_DEFINITIONS]}",
        }
