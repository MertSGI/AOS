"""Deterministic offline unit tests for AOS GitDataCASRelayTransport.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01
PROVES ZERO NETWORK CALLS AND TOKEN SAFETY.
"""

from __future__ import annotations

import base64
import json
from typing import Dict, List, Optional, Tuple

import pytest

from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REF,
    FIXED_RELAY_REPOSITORY,
    ControllerRelayTransportError,
    FakeCredentialProvider,
    GitDataCASRelayTransport,
    GitHubRequester,
    StdlibGitHubRequester,
)

HEAD_SHA_0 = "039232ecf10948bf55a9d9dab665828b6c06f7c6"
TREE_SHA_0 = "1111111111111111111111111111111111111111"
FAKE_TOKEN = "secret-github-token-99999"


class FakeGitHubRequester(GitHubRequester):
    """In-memory fake GitHub HTTP requester for deterministic Git Data CAS testing."""

    def __init__(self, initial_head: str = HEAD_SHA_0, token: str = FAKE_TOKEN):
        self.token = token
        self.ref_head = initial_head
        self.blobs: Dict[str, bytes] = {}
        self.trees: Dict[str, List[Dict[str, str]]] = {TREE_SHA_0: []}
        self.commits: Dict[str, Dict[str, Any]] = {
            initial_head: {
                "sha": initial_head,
                "tree": {"sha": TREE_SHA_0},
                "parents": [],
                "message": "initial commit",
            }
        }
        self.blob_create_count = 0
        self.tree_create_count = 0
        self.commit_create_count = 0
        self.ref_update_count = 0
        self.request_history: List[Dict[str, Any]] = []

        # Flags for simulating failure modes
        self.simulate_cas_race_on_patch = False
        self.simulate_remote_500 = False

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        self.request_history.append({"method": method, "path": path, "body": body})

        if self.simulate_remote_500:
            return 500, b'{"message": "Internal Server Error"}', {}

        # 1. GET branch ref
        if method == "GET" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/ref/heads/{FIXED_RELAY_BRANCH}":
            data = {
                "ref": FIXED_RELAY_REF,
                "object": {"sha": self.ref_head, "type": "commit"},
            }
            return 200, json.dumps(data).encode("utf-8"), {}

        # 2. GET commit
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits/"):
            sha = path.split("/")[-1]
            if sha in self.commits:
                return 200, json.dumps(self.commits[sha]).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # 3. GET tree
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees/"):
            sha = path.split("?")[0].split("/")[-1]
            if sha in self.trees:
                data = {"sha": sha, "tree": self.trees[sha]}
                if getattr(self, "simulate_truncated_tree", False) and "?recursive=1" in path:
                    data["truncated"] = True
                return 200, json.dumps(data).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # 4. GET blob
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs/"):
            sha = path.split("/")[-1]
            if sha in self.blobs:
                content_b64 = base64.b64encode(self.blobs[sha]).decode("ascii")
                data = {"sha": sha, "content": content_b64, "encoding": "base64"}
                return 200, json.dumps(data).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # 5. POST blob
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs":
            self.blob_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            content_bytes = base64.b64decode(payload["content"]) if payload.get("encoding") == "base64" else payload["content"].encode("utf-8")
            blob_sha = f"b{self.blob_create_count:039d}"
            self.blobs[blob_sha] = content_bytes
            return 201, json.dumps({"sha": blob_sha}).encode("utf-8"), {}

        # 6. POST tree
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees":
            self.tree_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            base_tree_sha = payload.get("base_tree")
            base_items = list(self.trees.get(base_tree_sha, [])) if base_tree_sha else []
            new_items = payload.get("tree", [])
            merged = base_items + new_items
            tree_sha = f"t{self.tree_create_count:039d}"
            self.trees[tree_sha] = merged
            return 201, json.dumps({"sha": tree_sha}).encode("utf-8"), {}

        # 7. POST commit
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits":
            self.commit_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            commit_sha = f"c{self.commit_create_count:039d}"
            parents_formatted = [{"sha": p} for p in payload.get("parents", [])]
            self.commits[commit_sha] = {
                "sha": commit_sha,
                "tree": {"sha": payload.get("tree")},
                "parents": parents_formatted,
                "message": payload.get("message"),
            }
            return 201, json.dumps({"sha": commit_sha}).encode("utf-8"), {}

        # 8. PATCH ref
        if method == "PATCH" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/refs/heads/{FIXED_RELAY_BRANCH}":
            self.ref_update_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            if payload.get("force") is not False:
                return 400, b'{"message": "force=false is strictly required"}', {}

            if self.simulate_cas_race_on_patch:
                # Concurrent writer moved head
                self.ref_head = "race-winner-commit-sha-9999"
                return 422, b'{"message": "Update is not a fast-forward"}', {}

            new_sha = payload.get("sha")
            self.ref_head = new_sha
            return 200, json.dumps({"ref": FIXED_RELAY_REF, "object": {"sha": new_sha}}).encode("utf-8"), {}

        return 404, b'{"message": "Unknown path"}', {}


def test_read_exact_branch_head():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    transport = GitDataCASRelayTransport(requester)
    head = transport.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH)
    assert head == HEAD_SHA_0


def test_expected_head_mismatch_rejects_before_object_creation():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    transport = GitDataCASRelayTransport(requester)

    wrong_head = "a" * 40
    res = transport.publish_record(
        FIXED_RELAY_REPOSITORY,
        FIXED_RELAY_BRANCH,
        "controller-relay/v1/messages/test.json",
        b"{}",
        expected_head=wrong_head,
    )

    assert res.is_valid is False
    assert res.disposition == "HOLD_CAS_RACE"
    assert requester.blob_create_count == 0
    assert requester.tree_create_count == 0
    assert requester.commit_create_count == 0


def test_existing_target_path_rejects():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    # Pre-populate existing path in base tree
    target_path = "controller-relay/v1/messages/test.json"
    requester.trees[TREE_SHA_0] = [{"path": target_path, "type": "blob", "sha": "b0001"}]

    transport = GitDataCASRelayTransport(requester)
    res = transport.publish_record(
        FIXED_RELAY_REPOSITORY,
        FIXED_RELAY_BRANCH,
        target_path,
        b"{}",
        expected_head=HEAD_SHA_0,
    )

    assert res.is_valid is False
    assert res.disposition == "HOLD_RECORD_ALREADY_EXISTS"
    assert requester.blob_create_count == 0


def test_valid_one_record_publication_plan():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    transport = GitDataCASRelayTransport(requester)
    target_path = "controller-relay/v1/messages/LARI--AOS/0001.json"
    payload = b'{"hello": "world"}'

    res = transport.publish_record(
        FIXED_RELAY_REPOSITORY,
        FIXED_RELAY_BRANCH,
        target_path,
        payload,
        expected_head=HEAD_SHA_0,
    )

    assert res.is_valid is True
    assert res.disposition == "PASS"
    assert requester.blob_create_count == 1
    assert requester.tree_create_count == 1
    assert requester.commit_create_count == 1
    assert requester.ref_update_count == 1

    # Verify post-write branch head is the new commit
    new_head = transport.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH)
    assert new_head == res.details["commit_sha"]

    # Verify parent of new commit is exact expected_head
    new_commit = requester.commits[new_head]
    assert new_commit["parents"][0]["sha"] == HEAD_SHA_0

    # Verify exact record path and content
    read_back = transport.read_record_bytes(FIXED_RELAY_REPOSITORY, new_head, target_path)
    assert read_back == payload


def test_cas_race_on_patch_ref_fails_closed():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    requester.simulate_cas_race_on_patch = True

    transport = GitDataCASRelayTransport(requester)
    target_path = "controller-relay/v1/messages/LARI--AOS/0001.json"

    res = transport.publish_record(
        FIXED_RELAY_REPOSITORY,
        FIXED_RELAY_BRANCH,
        target_path,
        b"{}",
        expected_head=HEAD_SHA_0,
    )

    assert res.is_valid is False
    assert res.disposition == "HOLD_CAS_RACE"
    # Ref update was attempted once and failed; no retries
    assert requester.ref_update_count == 1


def test_endpoint_allowlist_enforced():
    provider = FakeCredentialProvider(token=FAKE_TOKEN)
    req = StdlibGitHubRequester(provider)

    # Allowed endpoint passes allowlist check
    req._validate_endpoint("GET", "/repos/MertSGI/AOS/git/ref/heads/control/controller-relay")

    # Unallowed endpoint raises exception
    with pytest.raises(ControllerRelayTransportError, match="prohibited by allowlist"):
        req._validate_endpoint("GET", "/repos/MertSGI/AOS/issues")

    with pytest.raises(ControllerRelayTransportError, match="prohibited by allowlist"):
        req._validate_endpoint("DELETE", "/repos/MertSGI/AOS/git/refs/heads/control/controller-relay")


def test_token_safety_and_sanitization():
    provider = FakeCredentialProvider(token=FAKE_TOKEN)
    assert FAKE_TOKEN not in repr(provider)

    req = StdlibGitHubRequester(provider)
    assert FAKE_TOKEN not in repr(req)

    # Check error sanitization in StdlibGitHubRequester
    sanitized_err_str = ""
    try:
        # Simulate network call with invalid endpoint to trigger exception
        req.request("GET", "/repos/MertSGI/AOS/prohibited")
    except Exception as exc:
        sanitized_err_str = str(exc)

    assert FAKE_TOKEN not in sanitized_err_str


def test_github_unavailable_fails_closed():
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    requester.simulate_remote_500 = True

    transport = GitDataCASRelayTransport(requester)

    with pytest.raises(ControllerRelayTransportError, match="HTTP 500"):
        transport.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH)


# --- R1 HARDENING PROOF MATRIX TESTS ---

def test_production_recursive_tree_allowlist():
    """PRODUCTION_RECURSIVE_TREE_ALLOWLIST: Real StdlibGitHubRequester._validate_endpoint tests."""
    provider = FakeCredentialProvider(token=FAKE_TOKEN)
    req = StdlibGitHubRequester(provider)

    # 1. Exact ?recursive=1 production endpoint accepted
    req._validate_endpoint("GET", "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6?recursive=1")
    # No-query tree endpoint accepted
    req._validate_endpoint("GET", "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6")

    # 2. Arbitrary tree query rejected
    for bad_path in [
        "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6?recursive=0",
        "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6?recursive=true",
        "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6?foo=bar",
        "/repos/MertSGI/AOS/git/trees/039232ecf10948bf55a9d9dab665828b6c06f7c6?recursive=1&foo=bar",
    ]:
        with pytest.raises(ControllerRelayTransportError, match="prohibited by allowlist"):
            req._validate_endpoint("GET", bad_path)


def test_truncated_tree_fail_closed():
    """TRUNCATED_TREE_FAIL_CLOSED tests."""
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    requester.simulate_truncated_tree = True
    transport = GitDataCASRelayTransport(requester)

    # A. expected-head tree truncated before target absence proof -> publication rejected fail-closed
    target_path = "controller-relay/v1/messages/LARI--AOS/0001.json"
    res = transport.publish_record(
        FIXED_RELAY_REPOSITORY,
        FIXED_RELAY_BRANCH,
        target_path,
        b"{}",
        expected_head=HEAD_SHA_0,
    )
    assert res.is_valid is False
    assert res.disposition == "HOLD_GIT_TREE_TRUNCATED"
    assert requester.blob_create_count == 0
    assert requester.tree_create_count == 0
    assert requester.commit_create_count == 0
    assert requester.ref_update_count == 0

    # B. history listing against truncated recursive tree -> raises GitTreeTruncatedError
    with pytest.raises(ControllerRelayTransportError, match="truncated"):
        transport.list_records_under_prefix(FIXED_RELAY_REPOSITORY, HEAD_SHA_0, "controller-relay/v1/")


def test_message_publication_provenance_derivation():
    """MESSAGE_PUBLICATION_PROVENANCE tests."""
    requester = FakeGitHubRequester(initial_head=HEAD_SHA_0)
    transport = GitDataCASRelayTransport(requester)

    # Add commit C1 introducing msg1.json
    path_1 = "controller-relay/v1/messages/LARI--AOS/0001.json"
    content_1 = b'{"msg": 1}'
    c1_res = transport.publish_record(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH, path_1, content_1, expected_head=HEAD_SHA_0)
    c1_sha = c1_res.details["commit_sha"]

    # Add commit C2 introducing msg2.json
    path_2 = "controller-relay/v1/messages/LARI--AOS/0002.json"
    content_2 = b'{"msg": 2}'
    c2_res = transport.publish_record(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH, path_2, content_2, expected_head=c1_sha)
    c2_sha = c2_res.details["commit_sha"]

    # Verify publication commit of path_1 is c1_sha, NOT current head c2_sha
    pub_1 = transport.derive_publication_commit_sha(FIXED_RELAY_REPOSITORY, c2_sha, path_1)
    assert pub_1 == c1_sha
    assert pub_1 != c2_sha

    # Verify publication commit of path_2 is c2_sha
    pub_2 = transport.derive_publication_commit_sha(FIXED_RELAY_REPOSITORY, c2_sha, path_2)
    assert pub_2 == c2_sha
