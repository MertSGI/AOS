"""Comprehensive unit tests for LARI Controller Ingress connector surface & Interoperability.

Authority ID: LARI-AOS-AUTONOMOUS-QUALITY-LOOP-AND-RELAY-INGRESS-BOOTSTRAP-20260910-01
Hardening Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Tests:
1. Immutable principal binding: principal is always LARI_CONTROLLER and cannot be overridden.
2. Ingress operation allowlist enforcement.
3. relay_get_head operation.
4. relay_get_latest_unconsumed operation.
5. relay_read_message operation.
6. relay_publish_message with proper principal authentication.
7. relay_publish_receipt with proper principal authentication.
8. authority_fetch_and_verify operation on bounded path (.json).
9. publish_controller_authority operation on bounded path (.json).
10. Prohibited operations reject immediately.
11. Cross-component interoperability tests (A through P per section 7).
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
import pytest

from aos.controller_relay import (
    compute_message_content_sha256,
    format_message_id,
)
from aos.controller_relay_authority_resolver import (
    AuthorityArtifactResolver,
    AuthorityResolutionError,
    CanonicalAuthorityReference,
)
from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REPOSITORY,
    GitDataCASRelayTransport,
    GitHubRequester,
)
from aos.controller_relay_service import ControllerPrincipal, ControllerRelayService, derive_message_path, derive_receipt_path
from aos.lari_controller_ingress import (
    AUTHORITY_STORE_BRANCH,
    AUTHORITY_STORE_PATH_PREFIX,
    AUTHORITY_STORE_REPOSITORY,
    LARI_INGRESS_TOOL_DEFINITIONS,
    LariControllerIngressError,
    LariControllerIngressService,
    TransportSessionAuth,
    create_secret_session_authenticator,
    dispatch_lari_ingress_call,
)
from tests.test_controller_relay_cr1_once import InMemoryGitHubRequester, BOOTSTRAP_SHA

TEST_INGRESS_SECRET = "valid_secret_session_token_32_characters_long"


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
    authenticator = create_secret_session_authenticator(TEST_INGRESS_SECRET)
    ingress = LariControllerIngressService(
        relay_service=service,
        session_authenticator=authenticator,
        authority_requester=ctrl_requester,
    )
    return {
        "requester": requester,
        "transport": transport,
        "service": service,
        "ctrl_requester": ctrl_requester,
        "ingress": ingress,
        "secret": TEST_INGRESS_SECRET,
    }


def test_startup_fail_closed_without_authenticator(relay_stack):
    """Startup must fail closed if no authenticator is configured."""
    service = relay_stack["service"]
    with pytest.raises(LariControllerIngressError, match="STARTUP_FAIL_CLOSED"):
        LariControllerIngressService(
            relay_service=service,
            session_authenticator=None,  # type: ignore
        )


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
    auth_doc = {
        "authority_id": auth_id,
        "issuer_controller": "LARI_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": BOOTSTRAP_SHA,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "ALL_NON_PRODUCTION",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    content = json.dumps(auth_doc, indent=2)

    # 1. Publish authority
    pub_res = ingress.publish_controller_authority(
        authority_id=auth_id,
        content_json=content,
        expected_control_plane_head="11fed2e24e0c985072d0469679a9483cf1df66e8",
    )
    assert pub_res["status"] == "SUCCESS"
    assert pub_res["authority_id"] == auth_id
    assert pub_res["path"] == f"{AUTHORITY_STORE_PATH_PREFIX}{auth_id}.json"
    assert "authority_body_sha256" in pub_res

    # 2. Fetch and verify authority
    fetch_res = ingress.authority_fetch_and_verify(auth_id)
    assert fetch_res["status"] == "SUCCESS"
    assert fetch_res["authority_id"] == auth_id
    assert fetch_res["body"]["issuer_controller"] == "LARI_CONTROLLER"
    assert fetch_res["canonical_path"] == f"{AUTHORITY_STORE_PATH_PREFIX}{auth_id}.json"


def test_dispatch_allowed_and_disallowed_tools(relay_stack):
    ingress = relay_stack["ingress"]

    # Allowed tool dispatch with valid session token
    res = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=relay_stack["secret"])
    assert res["status"] == "SUCCESS"

    # Disallowed tool dispatch
    bad_res = dispatch_lari_ingress_call(ingress, "git_push_arbitrary", {}, session_token=relay_stack["secret"])
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

    # 1. Call with valid constant-time matching session token succeeds
    valid_token = relay_stack["secret"]
    res_valid = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=valid_token)
    assert res_valid["status"] == "SUCCESS"

    # 2. Call with invalid/short session token fails with UNAUTHORIZED
    invalid_token = "too_short"
    res_invalid = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=invalid_token)
    assert res_invalid["status"] == "UNAUTHORIZED"

    # 3. Call with arbitrary 32-character string fails with UNAUTHORIZED (no permissive fallback)
    arbitrary_token = "a" * 32
    res_arbitrary = dispatch_lari_ingress_call(ingress, "relay_get_head", {}, session_token=arbitrary_token)
    assert res_arbitrary["status"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Section 7 Cross-Component Interoperability Proofs (A through P)
# ---------------------------------------------------------------------------

def test_interop_a_b_c_d_lari_publishes_json_and_aos_resolver_validates(relay_stack):
    """A. LARI ingress publishes authority JSON.
    B. Published artifact path is exact .json canonical path.
    C. Publication returns exact artifact SHA256.
    D. AOS AuthorityArtifactResolver successfully resolves an artifact produced by LARI ingress.
    """
    ingress = relay_stack["ingress"]
    ctrl_requester = relay_stack["ctrl_requester"]

    auth_id = "LARI-AOS-INTEROP-AUTH-20260911-01"
    auth_doc = {
        "authority_id": auth_id,
        "issuer_controller": "LARI_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": BOOTSTRAP_SHA,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "TEST_SCOPE",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    content_json = json.dumps(auth_doc)

    pub_res = ingress.publish_controller_authority(
        authority_id=auth_id,
        content_json=content_json,
        expected_control_plane_head=ctrl_requester.head_sha,
    )

    # A, B, C
    assert pub_res["status"] == "SUCCESS"
    assert pub_res["path"] == f"docs/project-control/controller-authorities/LARI_CONTROLLER/{auth_id}.json"
    pub_commit = pub_res["publication_commit_sha"]
    body_sha256 = pub_res["authority_body_sha256"]
    assert len(body_sha256) == 64

    # D. AOS resolver independent resolution
    def aos_content_reader(repo: str, commit_sha: str, path: str) -> bytes:
        commit_data = json.loads(ctrl_requester.commits[commit_sha]["tree"]["sha"])
        tree_sha = commit_data
        for item in ctrl_requester.trees[tree_sha]:
            if item["path"] == path:
                return ctrl_requester.blobs[item["sha"]]
        raise FileNotFoundError(path)

    # Read bytes through ctrl_requester
    def clean_reader(repo: str, commit_sha: str, path: str) -> bytes:
        tree_sha = ctrl_requester.commits[commit_sha]["tree"]["sha"]
        for item in ctrl_requester.trees[tree_sha]:
            if item["path"] == path:
                return ctrl_requester.blobs[item["sha"]]
        raise FileNotFoundError(path)

    resolver = AuthorityArtifactResolver(content_reader=clean_reader)
    ref = CanonicalAuthorityReference(
        repository="MertSGI/Randapp-main",
        branch="control/lari-project-control-plane",
        publication_commit_sha=pub_commit,
        path=pub_res["path"],
        authority_id=auth_id,
        authority_body_sha256=body_sha256,
    )

    resolved = resolver.resolve_and_validate_authority(ref, expected_subject_sha=BOOTSTRAP_SHA)
    assert resolved["authority_id"] == auth_id
    assert resolved["issuer_controller"] == "LARI_CONTROLLER"


def test_interop_e_tampered_authority_bytes_fails_sha(relay_stack):
    """E. Tampered authority bytes fail SHA validation."""
    ctrl_requester = relay_stack["ctrl_requester"]
    ingress = relay_stack["ingress"]

    auth_id = "LARI-AOS-TAMPER-TEST-20260911-01"
    auth_doc = {
        "authority_id": auth_id,
        "issuer_controller": "LARI_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": BOOTSTRAP_SHA,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "TEST_SCOPE",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    pub_res = ingress.publish_controller_authority(auth_id, json.dumps(auth_doc), ctrl_requester.head_sha)
    assert pub_res["status"] == "SUCCESS"

    def tampered_reader(repo: str, commit_sha: str, path: str) -> bytes:
        return b'{"authority_id":"LARI-AOS-TAMPER-TEST-20260911-01","tampered":true}'

    resolver = AuthorityArtifactResolver(content_reader=tampered_reader)
    ref = CanonicalAuthorityReference(
        repository="MertSGI/Randapp-main",
        branch="control/lari-project-control-plane",
        publication_commit_sha=pub_res["publication_commit_sha"],
        path=pub_res["path"],
        authority_id=auth_id,
        authority_body_sha256=pub_res["authority_body_sha256"],
    )

    with pytest.raises(AuthorityResolutionError, match="mismatch"):
        resolver.resolve_and_validate_authority(ref)


def test_interop_f_g_h_invalid_authority_fields_fail(relay_stack):
    """F. Wrong issuer_controller fails.
    G. Wrong authority_id fails.
    H. Wrong expected subject SHA fails.
    """
    ctrl_requester = relay_stack["ctrl_requester"]
    ingress = relay_stack["ingress"]

    # F: Ingress rejects wrong issuer_controller at publication
    bad_issuer_doc = {
        "authority_id": "AUTH-01",
        "issuer_controller": "AOS_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": BOOTSTRAP_SHA,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "TEST_SCOPE",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    res_f = ingress.publish_controller_authority("AUTH-01", json.dumps(bad_issuer_doc), ctrl_requester.head_sha)
    assert res_f["status"] == "REJECTED"
    assert "issuer_controller" in res_f["error"]

    # G: Ingress rejects mismatched authority_id
    mismatch_id_doc = {
        "authority_id": "AUTH-DIFF",
        "issuer_controller": "LARI_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": BOOTSTRAP_SHA,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "TEST_SCOPE",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    res_g = ingress.publish_controller_authority("AUTH-01", json.dumps(mismatch_id_doc), ctrl_requester.head_sha)
    assert res_g["status"] == "REJECTED"
    assert "authority_id" in res_g["error"]

    # H: Wrong subject SHA rejected at resolver
    valid_doc = {
        "authority_id": "AUTH-01",
        "issuer_controller": "LARI_CONTROLLER",
        "subject_repository": "MertSGI/Randapp-main",
        "subject_branch": "control/lari-project-control-plane",
        "subject_sha": "a" * 40,
        "authority_class": "TEST_ONLY",
        "authorized_scope": "TEST_SCOPE",
        "forbidden_scope": "PRODUCTION_MUTATION",
        "production": "NO_GO",
        "status": "ACTIVE",
    }
    res_pub = ingress.publish_controller_authority("AUTH-01", json.dumps(valid_doc), ctrl_requester.head_sha)
    assert res_pub["status"] == "SUCCESS"

    def clean_reader(repo: str, commit_sha: str, path: str) -> bytes:
        tree_sha = ctrl_requester.commits[commit_sha]["tree"]["sha"]
        for item in ctrl_requester.trees[tree_sha]:
            if item["path"] == path:
                return ctrl_requester.blobs[item["sha"]]
        raise FileNotFoundError(path)

    resolver = AuthorityArtifactResolver(content_reader=clean_reader)
    ref = CanonicalAuthorityReference(
        repository="MertSGI/Randapp-main",
        branch="control/lari-project-control-plane",
        publication_commit_sha=res_pub["publication_commit_sha"],
        path=res_pub["path"],
        authority_id="AUTH-01",
        authority_body_sha256=res_pub["authority_body_sha256"],
    )
    with pytest.raises(AuthorityResolutionError, match="Subject SHA mismatch"):
        resolver.resolve_and_validate_authority(ref, expected_subject_sha="b" * 40)


def test_interop_i_markdown_artifact_rejected(relay_stack):
    """I. Markdown-only artifact is rejected as canonical authority."""
    ctrl_requester = relay_stack["ctrl_requester"]
    ingress = relay_stack["ingress"]

    # Publishing non-JSON markdown is rejected
    md_content = "# AUTHORITY\n\nAUTHORITY_ID=AUTH-MD\nISSUER_CONTROLLER=LARI_CONTROLLER\n"
    res = ingress.publish_controller_authority("AUTH-MD", md_content, ctrl_requester.head_sha)
    assert res["status"] == "REJECTED"
    assert "JSON" in res["error"]


def test_interop_j_k_l_m_n_strict_detector_delegation(relay_stack):
    """J. LARI ingress latest-unconsumed delegates to strict detector semantics.
    K. Superseded inbound message is not selected.
    L. Invalid consumed lifecycle fails closed.
    M. requires_reply integrity is preserved.
    N. Git publication ordinal governs selection.
    """
    ingress = relay_stack["ingress"]
    service = relay_stack["service"]
    p_aos = ControllerPrincipal("AOS_CONTROLLER")

    # Publish Message 1 from AOS to LARI
    msg1 = {
        "schema_version": "0.1",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "sequence": 1,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "created_at": "2026-09-11T07:00:00Z",
        "subject": "MSG_1",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "control/controller-relay",
        "subject_sha": BOOTSTRAP_SHA,
        "decision": "STEP_1",
        "authority_effect": "NONE",
        "authority_refs": ["REF-01"],
        "requested_next_action": "REPLY",
        "requires_reply": False,
    }
    msg1["content_sha256"] = compute_message_content_sha256(msg1)
    res1 = service.publish_message(json.dumps(msg1).encode("utf-8"), service.get_head(), p_aos)
    assert res1.is_valid

    # Check J & N: Message 1 is detected as latest
    unconsumed1 = ingress.relay_get_latest_unconsumed()
    assert unconsumed1["status"] == "SUCCESS"
    assert unconsumed1["latest_unconsumed"]["message_id"] == msg1["message_id"]

    # Publish Message 2 superseding Message 1 (K)
    msg2 = {
        "schema_version": "0.1",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000002",
        "thread_id": msg1["thread_id"],
        "sequence": 2,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "supersedes_message_id": msg1["message_id"],
        "created_at": "2026-09-11T07:05:00Z",
        "subject": "MSG_2",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "control/controller-relay",
        "subject_sha": BOOTSTRAP_SHA,
        "decision": "STEP_2",
        "authority_effect": "NONE",
        "authority_refs": ["REF-01"],
        "requested_next_action": "REPLY",
        "requires_reply": True,
    }
    msg2["content_sha256"] = compute_message_content_sha256(msg2)
    res2 = service.publish_message(json.dumps(msg2).encode("utf-8"), service.get_head(), p_aos)
    assert res2.is_valid

    # K: Superseded Msg1 excluded, Msg2 selected
    unconsumed2 = ingress.relay_get_latest_unconsumed()
    assert unconsumed2["status"] == "SUCCESS"
    assert unconsumed2["latest_unconsumed"]["message_id"] == msg2["message_id"]

    # M: Message 2 requires_reply=True. LARI publishing CONSUMED without reply is rejected by service
    rcpt_c = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": msg2["message_id"],
        "message_commit_sha": res2.details["commit_sha"],
        "message_content_sha256": msg2["content_sha256"],
        "actor": "LARI_CONTROLLER",
        "event": "CONSUMED",
        "created_at": "2026-09-11T07:10:00Z",
    }
    # OBSERVED -> VERIFIED -> ACKNOWLEDGED
    for ev in ["OBSERVED", "VERIFIED", "ACKNOWLEDGED"]:
        r = dict(rcpt_c)
        r["event"] = ev
        ingress.relay_publish_receipt(json.dumps(r), service.get_head())

    # Direct attempt to publish CONSUMED without outbound reply fails closed (L, M)
    con_res = ingress.relay_publish_receipt(json.dumps(rcpt_c), service.get_head())
    assert con_res["status"] == "FAILED"


def test_interop_o_p_principal_and_authority_effect_invariants(relay_stack):
    """O. Principal remains immutable LARI_CONTROLLER.
    P. Relay authority_effect remains NONE.
    """
    ingress = relay_stack["ingress"]
    assert ingress.principal_controller_id == "LARI_CONTROLLER"

    # Attempting to publish message as AOS_CONTROLLER from LARI ingress fails authentication
    bad_msg = {
        "schema_version": "0.1",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "sequence": 1,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "created_at": "2026-09-11T07:00:00Z",
        "subject": "SPOOF",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "control/controller-relay",
        "subject_sha": BOOTSTRAP_SHA,
        "decision": "SPOOF",
        "authority_effect": "NONE",
        "authority_refs": ["REF-01"],
        "requested_next_action": "NONE",
        "requires_reply": False,
    }
    bad_msg["content_sha256"] = compute_message_content_sha256(bad_msg)
    res = ingress.relay_publish_message(json.dumps(bad_msg), relay_stack["service"].get_head())
    assert res["status"] == "FAILED"
    assert "Authentication mismatch" in str(res["errors"])

    # Attempting to publish message with authority_effect != NONE fails protocol validation (P)
    bad_auth_effect_msg = dict(bad_msg)
    bad_auth_effect_msg["from"] = "LARI_CONTROLLER"
    bad_auth_effect_msg["to"] = "AOS_CONTROLLER"
    bad_auth_effect_msg["message_id"] = "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001"
    bad_auth_effect_msg["thread_id"] = bad_auth_effect_msg["message_id"]
    bad_auth_effect_msg["authority_effect"] = "GRANT_AUTHORITY"
    bad_auth_effect_msg["content_sha256"] = compute_message_content_sha256(bad_auth_effect_msg)
    res_p = ingress.relay_publish_message(json.dumps(bad_auth_effect_msg), relay_stack["service"].get_head())
    assert res_p["status"] == "FAILED"
    assert any("authority_effect" in err.lower() for err in res_p["errors"])
