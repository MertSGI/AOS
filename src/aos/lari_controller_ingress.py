"""LARI Controller ChatGPT/MCP Ingress Surface around ControllerRelayService.

Implementation Authority ID: LARI-AOS-AUTONOMOUS-QUALITY-LOOP-AND-RELAY-INGRESS-BOOTSTRAP-20260910-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

This module provides a strictly bounded, authenticated MCP and ChatGPT connector surface
for the LARI_CONTROLLER principal.

Allowed operations:
1. relay_get_head() -> Dict[str, Any]
2. relay_get_latest_unconsumed() -> Dict[str, Any]
3. relay_read_message(message_id: str) -> Dict[str, Any]
4. relay_publish_message(raw_json_str: str, expected_head: str) -> Dict[str, Any]
5. relay_publish_receipt(raw_json_str: str, expected_head: str) -> Dict[str, Any]
6. authority_fetch_and_verify(authority_id: str, exact_commit_sha: Optional[str] = None) -> Dict[str, Any]
7. publish_controller_authority(authority_id: str, content_markdown: str, expected_control_plane_head: str) -> Dict[str, Any]

Security Invariants:
- Principal binding is IMMUTABLE and hardcoded to LARI_CONTROLLER for this ingress surface.
- Callers (AOS_CONTROLLER / AG / external) CANNOT pass or override the principal parameter.
- Mutation uses exclusively the dedicated GitHub App installation identity via GitDataCASRelayTransport.
- Authority publication is strictly bounded to repository MertSGI/Randapp-main, branch control/lari-project-control-plane,
  under path docs/project-control/controller-authorities/LARI_CONTROLLER/<AUTHORITY_ID>.md.
- No generic Git operations. No arbitrary path writes. No product branch writes.
- RELAY_MESSAGE != AUTHORITY, RELAY_AUTHORITY_EFFECT=NONE.
"""

from __future__ import annotations

import base64
import json
import re
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
AUTHORITY_ID_REGEX = re.compile(r"^[A-Z0-9_\-]+$")
GIT_SHA_REGEX = re.compile(r"^[0-9a-f]{40}$")


class LariControllerIngressError(Exception):
    """Base exception for LARI Controller Ingress failures."""
    pass


class LariControllerIngressService:
    """Narrowly scoped, authenticated LARI_CONTROLLER connector surface."""

    def __init__(
        self,
        relay_service: ControllerRelayService,
        authority_requester: Optional[GitHubRequester] = None,
    ):
        """Initialize with an active ControllerRelayService and optional authority requester.

        Note: Principal is permanently bound to LARI_CONTROLLER.
        """
        self._relay_service = relay_service
        self._principal = LARI_CONTROLLER_PRINCIPAL
        self._authority_requester = authority_requester

    @property
    def principal_controller_id(self) -> str:
        """Authoritative immutable caller identity."""
        return self._principal.controller_id

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
        """Find the latest message sent to LARI_CONTROLLER that has not been CONSUMED by LARI_CONTROLLER."""
        # Principal check: this ingress only serves LARI_CONTROLLER
        target_recipient = self._principal.controller_id
        if for_controller and for_controller != target_recipient:
            raise ValueError(
                f"Ingress principal violation: cannot query unconsumed messages for '{for_controller}' (bound to '{target_recipient}')"
            )

        try:
            head_sha = self._relay_service.get_head()
            msg_provenances = self._relay_service.list_message_provenances(ref=head_sha)
            receipt_provenances = self._relay_service.list_receipt_provenances(ref=head_sha)

            # Collect all message IDs consumed by LARI_CONTROLLER
            consumed_msg_ids = set()
            for rcpt, _ in receipt_provenances:
                if rcpt.get("actor") == target_recipient and rcpt.get("event") == "CONSUMED":
                    consumed_msg_ids.add(rcpt.get("message_id"))

            # Filter messages addressed to LARI_CONTROLLER that have not been CONSUMED by LARI_CONTROLLER
            unconsumed = []
            for msg, pub_sha in msg_provenances:
                if msg.get("to") == target_recipient:
                    msg_id = msg.get("message_id")
                    if msg_id not in consumed_msg_ids:
                        unconsumed.append({
                            "message": msg,
                            "publication_commit_sha": pub_sha,
                        })

            # Sort by sequence ascending, or return latest
            if not unconsumed:
                return {
                    "status": "EMPTY",
                    "head_sha": head_sha,
                    "target_controller": target_recipient,
                    "unconsumed_count": 0,
                    "latest_unconsumed": None,
                }

            # Return latest unconsumed
            latest = unconsumed[-1]
            return {
                "status": "SUCCESS",
                "head_sha": head_sha,
                "target_controller": target_recipient,
                "unconsumed_count": len(unconsumed),
                "latest_unconsumed": latest["message"],
                "publication_commit_sha": latest["publication_commit_sha"],
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
            "commit_sha": res.details.get("commit_sha"),
            "path": res.details.get("path"),
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
            "commit_sha": res.details.get("commit_sha"),
            "path": res.details.get("path"),
        }

    # --------------------------------------------------------------------------
    # 6. authority_fetch_and_verify
    # --------------------------------------------------------------------------
    def authority_fetch_and_verify(
        self,
        authority_id: str,
        exact_commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch and verify an immutable Controller authority document from canonical control plane."""
        if not isinstance(authority_id, str) or not AUTHORITY_ID_REGEX.match(authority_id):
            return {"status": "ERROR", "error": f"Invalid authority_id format: '{authority_id}'"}

        rel_path = f"{AUTHORITY_STORE_PATH_PREFIX}{authority_id}.md"

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

            # 2. Fetch commit to find tree
            commit_path = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/commits/{target_sha}"
            st, body, _ = self._authority_requester.request("GET", commit_path)
            if st != 200:
                return {"status": "ERROR", "error": f"Failed to fetch commit '{target_sha}' (HTTP {st})"}
            commit_data = json.loads(body.decode("utf-8"))
            tree_sha = commit_data["tree"]["sha"]

            # 3. Fetch tree recursive
            tree_path = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/trees/{tree_sha}?recursive=1"
            st, body, _ = self._authority_requester.request("GET", tree_path)
            if st != 200:
                return {"status": "ERROR", "error": f"Failed to fetch tree '{tree_sha}' (HTTP {st})"}
            tree_data = json.loads(body.decode("utf-8"))

            blob_sha = None
            for item in tree_data.get("tree", []):
                if item.get("path") == rel_path and item.get("type") == "blob":
                    blob_sha = item.get("sha")
                    break

            if not blob_sha:
                return {
                    "status": "NOT_FOUND",
                    "authority_id": authority_id,
                    "target_path": rel_path,
                    "commit_sha": target_sha,
                }

            # 4. Fetch blob
            blob_url = f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/blobs/{blob_sha}"
            st, body, _ = self._authority_requester.request("GET", blob_url)
            if st != 200:
                return {"status": "ERROR", "error": f"Failed to fetch blob '{blob_sha}' (HTTP {st})"}
            blob_data = json.loads(body.decode("utf-8"))
            encoding = blob_data.get("encoding")
            content_str = blob_data.get("content", "")

            if encoding == "base64":
                raw_bytes = base64.b64decode(content_str.replace("\n", "").replace("\r", ""))
            else:
                raw_bytes = content_str.encode("utf-8")

            content_text = raw_bytes.decode("utf-8")

            # Verify that authority document contains its own authority ID
            if authority_id not in content_text:
                return {
                    "status": "VERIFICATION_FAILED",
                    "error": f"Authority artifact at '{rel_path}' does not declare authority_id '{authority_id}'",
                }

            return {
                "status": "SUCCESS",
                "authority_id": authority_id,
                "commit_sha": target_sha,
                "blob_sha": blob_sha,
                "path": rel_path,
                "content": content_text,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "error": str(exc),
            }

    # --------------------------------------------------------------------------
    # 7. publish_controller_authority
    # --------------------------------------------------------------------------
    def publish_controller_authority(
        self,
        authority_id: str,
        content_markdown: str,
        expected_control_plane_head: str,
    ) -> Dict[str, Any]:
        """Publish an immutable Controller authority document directly to canonical control plane.

        Destination repository: MertSGI/Randapp-main
        Destination branch: control/lari-project-control-plane
        Destination path: docs/project-control/controller-authorities/LARI_CONTROLLER/<AUTHORITY_ID>.md
        """
        if not isinstance(authority_id, str) or not AUTHORITY_ID_REGEX.match(authority_id):
            return {"status": "ERROR", "error": f"Invalid authority_id format: '{authority_id}'"}

        if not isinstance(content_markdown, str) or not content_markdown.strip():
            return {"status": "ERROR", "error": "content_markdown must be a non-empty string"}

        if authority_id not in content_markdown:
            return {
                "status": "ERROR",
                "error": f"Authority markdown content must contain authority_id '{authority_id}'",
            }

        if not isinstance(expected_control_plane_head, str) or not GIT_SHA_REGEX.match(expected_control_plane_head):
            return {"status": "ERROR", "error": f"Invalid expected_control_plane_head SHA: '{expected_control_plane_head}'"}

        if not self._authority_requester:
            return {"status": "ERROR", "error": "Authority requester not configured for authority publication"}

        target_path = f"{AUTHORITY_STORE_PATH_PREFIX}{authority_id}.md"

        try:
            # 1. Verify current ref head matches expected_control_plane_head
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

            # 3. Create blob for authority markdown
            content_bytes = content_markdown.encode("utf-8")
            b64_content = base64.b64encode(content_bytes).decode("ascii")
            blob_req_body = json.dumps({"content": b64_content, "encoding": "base64"}).encode("utf-8")
            st, body, _ = self._authority_requester.request("POST", f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/blobs", body=blob_req_body)
            if st != 201:
                return {"status": "ERROR", "error": f"Failed to create blob for authority document (HTTP {st})"}
            new_blob_sha = json.loads(body.decode("utf-8"))["sha"]

            # 4. Create new tree
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

            # 6. CAS patch branch ref
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
                "commit_sha": new_commit_sha,
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
        "description": "Retrieve the latest unconsumed Relay message addressed to LARI_CONTROLLER.",
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
        "description": "Fetch and verify an immutable Controller authority document from the project control plane.",
        "input_schema": {
            "type": "object",
            "properties": {
                "authority_id": {"type": "string", "description": "Identifier of the controller authority"},
                "exact_commit_sha": {"type": "string", "description": "Optional exact 40-hex commit SHA to pin verification"},
            },
            "required": ["authority_id"],
        },
    },
    {
        "name": "publish_controller_authority",
        "description": "Publish an immutable Controller authority markdown document to the project control plane repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "authority_id": {"type": "string", "description": "Identifier of the controller authority"},
                "content_markdown": {"type": "string", "description": "Exact markdown content of the authority artifact"},
                "expected_control_plane_head": {"type": "string", "description": "Exact 40-hex commit SHA of control/lari-project-control-plane HEAD"},
            },
            "required": ["authority_id", "content_markdown", "expected_control_plane_head"],
        },
    },
]


def dispatch_lari_ingress_call(
    service: LariControllerIngressService,
    tool_name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Dispatch an inbound tool call from ChatGPT / MCP to LariControllerIngressService.

    Enforces tool allowlist and parameter boundaries.
    """
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
        )
    elif tool_name == "publish_controller_authority":
        return service.publish_controller_authority(
            authority_id=arguments.get("authority_id", ""),
            content_markdown=arguments.get("content_markdown", ""),
            expected_control_plane_head=arguments.get("expected_control_plane_head", ""),
        )
    else:
        return {
            "status": "ERROR",
            "error": f"Unauthorized tool operation '{tool_name}'. Allowed operations: {[t['name'] for t in LARI_INGRESS_TOOL_DEFINITIONS]}",
        }
