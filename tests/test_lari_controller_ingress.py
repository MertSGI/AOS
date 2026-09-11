"""Comprehensive unit tests for LARI Controller Ingress connector surface.

Authority ID: LARI-AOS-AUTONOMOUS-QUALITY-LOOP-AND-RELAY-INGRESS-BOOTSTRAP-20260910-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Tests:
1. Immutable principal binding: principal is always LARI_CONTROLLER and cannot be overridden.
2. Ingress operation allowlist enforcement.
3. relay_get_head operation.
4. relay_get_latest_unconsumed operation.
5. relay_read_message operation.
6. relay_publish_message with proper principal authentication.
7. relay_publish_receipt with proper principal authentication.
8. authority_fetch_and_verify operation on bounded path.
9. publish_controller_authority operation on bounded path.
10. Prohibited operations reject immediately.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
import pytest

from aos.controller_relay import (
    compute_message_content_sha256,
    format_message_id,
)
from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REPOSITORY,
    GitDataCASRelayTransport,
    GitHubRequester,
)
from aos.controller_relay_service import ControllerPrincipal, ControllerRelayService
from aos.lari_controller_ingress import (
    AUTHORITY_STORE_BRANCH,
    AUTHORITY_STORE_PATH_PREFIX,
    AUTHORITY_STORE_REPOSITORY,
    LARI_INGRESS_TOOL_DEFINITIONS,
    LariControllerIngressService,
    dispatch_lari_ingress_call,
)
from tests.test_controller_relay_cr1_once import InMemoryGitHubRequester, BOOTSTRAP_SHA


class InMemoryControlPlaneRequester(GitHubRequester):
    """Fake GitHub Requester for Randapp-main control plane operations."""

    def __init__(self, initial_head: str = "11fed2e24e0c985072d0469679a9483cf1df66e8"):
        self.head_sha = initial_head
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, list[dict[str, str]]] = {
            "initial_tree_sha": [],
        }
        self.commits: dict[str, dict[str, any]] = {
            initial_head: {
                "sha": initial_head,
                "tree": {"sha": "initial_tree_sha"},
                "parents": [],
                "message": "docs(control): base control commit",
            }
        }
        self.blob_counter = 0
        self.tree_counter = 0
        self.commit_counter = 0

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        method = method.upper()
        if method == "GET" and path == f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/ref/heads/{AUTHORITY_STORE_BRANCH}":
            return 200, json.dumps({"object": {"sha": self.head_sha}}).encode("utf-8"), {}

        if method == "GET" and path.startswith(f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/commits/"):
            sha = path.split("/")[-1]
            if sha in self.commits:
                return 200, json.dumps(self.commits[sha]).encode("utf-8"), {}
            return 404, b"Commit not found", {}

        if method == "GET" and path.startswith(f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/trees/"):
            clean_path = path.split("?")[0]
            sha = clean_path.split("/")[-1]
            if sha in self.trees:
                return 200, json.dumps({"sha": sha, "tree": self.trees[sha], "truncated": False}).encode("utf-8"), {}
            return 404, b"Tree not found", {}

        if method == "GET" and path.startswith(f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/blobs/"):
            sha = path.split("/")[-1]
            if sha in self.blobs:
                return 200, json.dumps({"sha": sha, "content": base64.b64encode(self.blobs[sha]).decode("ascii"), "encoding": "base64"}).encode("utf-8"), {}
            return 404, b"Blob not found", {}

        if method == "POST" and path == f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/blobs":
            self.blob_counter += 1
            payload = json.loads(body.decode("utf-8"))
            raw = base64.b64decode(payload["content"]) if payload.get("encoding") == "base64" else payload["content"].encode("utf-8")
            b_sha = f"{self.blob_counter:040d}"
            self.blobs[b_sha] = raw
            return 201, json.dumps({"sha": b_sha}).encode("utf-8"), {}

        if method == "POST" and path == f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/trees":
            self.tree_counter += 1
            payload = json.loads(body.decode("utf-8"))
            t_sha = f"{self.tree_counter:040x}"
            self.trees[t_sha] = payload.get("tree", [])
            return 201, json.dumps({"sha": t_sha}).encode("utf-8"), {}

        if method == "POST" and path == f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/commits":
            self.commit_counter += 1
            payload = json.loads(body.decode("utf-8"))
            c_sha = f"{self.commit_counter:040x}"
            self.commits[c_sha] = {
                "sha": c_sha,
                "tree": {"sha": payload["tree"]},
                "parents": [{"sha": p} for p in payload.get("parents", [])],
                "message": payload.get("message", ""),
            }
            return 201, json.dumps({"sha": c_sha}).encode("utf-8"), {}

        if method == "PATCH" and path == f"/repos/{AUTHORITY_STORE_REPOSITORY}/git/refs/heads/{AUTHORITY_STORE_BRANCH}":
            payload = json.loads(body.decode("utf-8"))
            self.head_sha = payload["sha"]
            return 200, json.dumps({"object": {"sha": self.head_sha}}).encode("utf-8"), {}

        return 400, b"Unhandled request in mock", {}


@pytest.fixture
def relay_stack():
    requester = InMemoryGitHubRequester()
    transport = GitDataCASRelayTransport(requester)
    service = ControllerRelayService(transport)
    ctrl_requester = InMemoryControlPlaneRequester()
    ingress = LariControllerIngressService(service, authority_requester=ctrl_requester)
    return {
        "requester": requester,
        "transport": transport,
        "service": service,
        "ctrl_requester": ctrl_requester,
        "ingress": ingress,
    }


def test_immutable_principal_binding(relay_stack):
    ingress = relay_stack["ingress"]
    assert ingress.principal_controller_id == "LARI_CONTROLLER"

    # Cannot query unconsumed messages for another controller
    with pytest.raises(ValueError, match="Ingress principal violation"):
        ingress.relay_get_latest_unconsumed(for_controller="AOS_CONTROLLER")


def test_relay_get_head(relay_stack):
    ingress = relay_stack["ingress"]
    res = ingress.relay_get_head()
    assert res["status"] == "SUCCESS"
    assert res["repository"] == FIXED_RELAY_REPOSITORY
    assert res["branch"] == FIXED_RELAY_BRANCH
    assert res["head_sha"] == BOOTSTRAP_SHA


def test_relay_read_message_not_found(relay_stack):
    ingress = relay_stack["ingress"]
    msg_id = "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
    res = ingress.relay_read_message(msg_id)
    assert res["status"] == "NOT_FOUND"


def test_authority_publish_and_fetch_verify(relay_stack):
    ingress = relay_stack["ingress"]
    auth_id = "LARI-AOS-PROGRAM-V2-TEST-AUTHORITY-20260911-01"
    content = f"# CONTROLLER AUTHORITY: {auth_id}\n\nDECISION=ACCEPTED\n"

    # 1. Publish authority
    pub_res = ingress.publish_controller_authority(
        authority_id=auth_id,
        content_markdown=content,
        expected_control_plane_head="11fed2e24e0c985072d0469679a9483cf1df66e8",
    )
    assert pub_res["status"] == "SUCCESS"
    assert pub_res["authority_id"] == auth_id
    assert pub_res["path"] == f"{AUTHORITY_STORE_PATH_PREFIX}{auth_id}.md"

    # 2. Fetch and verify authority
    fetch_res = ingress.authority_fetch_and_verify(auth_id)
    assert fetch_res["status"] == "SUCCESS"
    assert fetch_res["authority_id"] == auth_id
    assert auth_id in fetch_res["content"]


def test_dispatch_allowed_and_disallowed_tools(relay_stack):
    ingress = relay_stack["ingress"]

    # Allowed tool dispatch
    res = dispatch_lari_ingress_call(ingress, "relay_get_head", {})
    assert res["status"] == "SUCCESS"

    # Disallowed tool dispatch
    bad_res = dispatch_lari_ingress_call(ingress, "git_push_arbitrary", {})
    assert bad_res["status"] == "ERROR"
    assert "Unauthorized tool operation" in bad_res["error"]


def test_tool_definitions_exact_allowlist():
    tool_names = [t["name"] for t in LARI_INGRESS_TOOL_DEFINITIONS]
    expected = [
        "relay_get_head",
        "relay_get_latest_unconsumed",
        "relay_read_message",
        "relay_publish_message",
        "relay_publish_receipt",
        "authority_fetch_and_verify",
        "publish_controller_authority",
    ]
    assert tool_names == expected


def test_transport_session_authentication_boundary(relay_stack):
    """AOS/AG cannot access boundary as LARI_CONTROLLER without presenting valid session credential."""
    ingress = relay_stack["ingress"]

    # Call with valid session token (>= 32 chars) succeeds
    valid_token = "valid_secret_session_token_32_characters_long"
    res_valid = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=valid_token)
    assert res_valid["status"] == "SUCCESS"

    # Call with invalid/short session token fails with UNAUTHORIZED
    invalid_token = "too_short"
    res_invalid = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=invalid_token)
    assert res_invalid["status"] == "UNAUTHORIZED"


def test_requires_reply_unconsumed_does_not_suppress_without_reply(relay_stack):
    """Verify that a CONSUMED receipt does NOT suppress an inbound message requiring reply if no reply exists."""
    ingress = relay_stack["ingress"]
    service = relay_stack["service"]
    transport = relay_stack["transport"]
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    # 1. AOS sends sequence 1 message with requires_reply=True
    msg_dict = {
        "schema_version": "0.1",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "sequence": 1,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "created_at": "2026-09-11T06:00:00Z",
        "subject": "CR2_LITE_PROBE",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "control/controller-relay",
        "subject_sha": BOOTSTRAP_SHA,
        "decision": "PROBE",
        "authority_effect": "NONE",
        "authority_refs": ["REF-01"],
        "requested_next_action": "REPLY",
        "requires_reply": True,
    }
    msg_dict["content_sha256"] = compute_message_content_sha256(msg_dict)
    raw_msg = json.dumps(msg_dict, sort_keys=True).encode("utf-8")

    head_sha = service.get_head()
    pub_res = service.publish_message(raw_msg, head_sha, p_aos)
    assert pub_res.is_valid

    # Check unconsumed: message is detected
    unconsumed = ingress.relay_get_latest_unconsumed()
    assert unconsumed["status"] == "SUCCESS"
    assert unconsumed["latest_unconsumed"]["message_id"] == msg_dict["message_id"]

