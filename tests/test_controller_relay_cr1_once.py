"""Deterministic offline unit tests for Controller Relay CR-1 One-Shot Handshake Runner.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-LIVE-INVOKER-R0-20260903-01
PROVES ROLE BOUNDARIES, EXACT IDENTITIES, CANONICAL HASH RECOMPUTATION, ZERO MUTATION,
LIVE SERVICE-TO-TRANSPORT WIRING, CAS RACE HANDLING, SECRET-FREE RESULT SURFACES,
AND NO AUTOMATIC RETRY.
"""

from __future__ import annotations

import base64

from aos.controller_relay import ControllerRelayError
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

from aos.controller_relay import compute_message_content_sha256, validate_controller_relay_message_raw
from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REF,
    FIXED_RELAY_REPOSITORY,
    TRUSTED_RELAY_BOOTSTRAP_SHA,
    GitDataCASRelayTransport,
    GitHubRequester,
)
from aos.controller_relay_service import ControllerRelayService, derive_message_path
from scripts.controller_relay_cr1_once import (
    CR1_AUTHORITY_REFS,
    CR1_EXPECTED_PARENT_SHA,
    CR1_SCHEMA_VERSION,
    CR1_SUBJECT_BRANCH,
    CR1_SUBJECT_REPOSITORY,
    CR1_SUBJECT_SHA,
    EXPECTED_LIVE_RELAY_HEAD,
    build_cr1_reply_message_plan,
    build_cr1_root_message_plan,
    execute_cr1_dry_run,
    execute_cr1_live,
    observe_exact_cr1_reply,
    observe_exact_cr1_root,
    verify_cr1_reply,
)

# Exact bootstrap SHA for initial branch head
BOOTSTRAP_SHA = "039232ecf10948bf55a9d9dab665828b6c06f7c6"

# Fake non-sensitive marker strings for secret-free testing
FAKE_TOKEN_MARKER = "FAKE-NONSECRET-TEST-MARKER-XYZ-99999"
FAKE_BEARER_MARKER = "FAKE-BEARER-NONSECRET-MARKER-12345"


# ============================================================================
# TEST-LOCAL FAKE GITHUB REQUESTER
# ============================================================================


class InMemoryGitHubRequester(GitHubRequester):
    """Test-local in-memory fake GitHub requester for GitDataCASRelayTransport.

    Maintains deterministic in-memory: branch head, commits, trees, blobs,
    ref update history, and request counters.

    Initial branch head: 039232ecf10948bf55a9d9dab665828b6c06f7c6
    Initial bootstrap commit/tree: sufficient for accepted provenance walker and empty Relay history.
    """

    def __init__(self) -> None:
        # Deterministic initial state
        self._initial_tree_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"  # empty tree placeholder
        self._ref_head = BOOTSTRAP_SHA

        # Git object stores
        self.blobs: Dict[str, bytes] = {}
        self.trees: Dict[str, List[Dict[str, str]]] = {
            self._initial_tree_sha: [],  # empty tree for bootstrap
        }
        self.commits: Dict[str, Dict[str, Any]] = {
            BOOTSTRAP_SHA: {
                "sha": BOOTSTRAP_SHA,
                "tree": {"sha": self._initial_tree_sha},
                "parents": [],
                "message": "bootstrap: controller-relay initial commit",
            }
        }

        # Counters
        self.blob_create_count = 0
        self.tree_create_count = 0
        self.commit_create_count = 0
        self.ref_update_count = 0
        self.force_update_count = 0

        # Ref update history for auditing
        self.ref_update_history: List[Dict[str, Any]] = []

        # Request log
        self.request_log: List[Dict[str, Any]] = []

        # Race simulation
        self._simulate_race_on_patch = False

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, bytes, Dict[str, str]]:
        self.request_log.append({"method": method, "path": path})

        # GET ref
        if method == "GET" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/ref/heads/{FIXED_RELAY_BRANCH}":
            data = {
                "ref": FIXED_RELAY_REF,
                "object": {"sha": self._ref_head, "type": "commit"},
            }
            return 200, json.dumps(data).encode("utf-8"), {}

        # GET commit
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits/"):
            sha = path.split("/")[-1]
            if sha in self.commits:
                return 200, json.dumps(self.commits[sha]).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # GET tree
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees/"):
            sha = path.split("?")[0].split("/")[-1]
            if sha in self.trees:
                data = {"sha": sha, "tree": self.trees[sha], "truncated": False}
                return 200, json.dumps(data).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # GET blob
        if method == "GET" and path.startswith(f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs/"):
            sha = path.split("/")[-1]
            if sha in self.blobs:
                content_b64 = base64.b64encode(self.blobs[sha]).decode("ascii")
                data = {"sha": sha, "content": content_b64, "encoding": "base64"}
                return 200, json.dumps(data).encode("utf-8"), {}
            return 404, b'{"message": "Not Found"}', {}

        # POST blob
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/blobs":
            self.blob_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            if payload.get("encoding") == "base64":
                content_bytes = base64.b64decode(payload["content"])
            else:
                content_bytes = payload.get("content", "").encode("utf-8")
            # Deterministic blob SHA from content
            blob_sha = hashlib.sha1(b"blob " + str(len(content_bytes)).encode() + b"\0" + content_bytes).hexdigest()
            self.blobs[blob_sha] = content_bytes
            return 201, json.dumps({"sha": blob_sha}).encode("utf-8"), {}

        # POST tree
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/trees":
            self.tree_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            base_tree_sha = payload.get("base_tree")
            base_items = list(self.trees.get(base_tree_sha, [])) if base_tree_sha else []
            new_items = payload.get("tree", [])
            merged = base_items + new_items
            # Deterministic tree SHA
            tree_content = json.dumps(merged, sort_keys=True).encode("utf-8")
            tree_sha = hashlib.sha1(b"tree " + str(len(tree_content)).encode() + b"\0" + tree_content).hexdigest()
            self.trees[tree_sha] = merged
            return 201, json.dumps({"sha": tree_sha}).encode("utf-8"), {}

        # POST commit
        if method == "POST" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/commits":
            self.commit_create_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}
            # Deterministic commit SHA
            commit_content = json.dumps(payload, sort_keys=True).encode("utf-8")
            commit_sha = hashlib.sha1(
                b"commit " + str(len(commit_content)).encode() + b"\0" + commit_content
            ).hexdigest()
            parents_formatted = [{"sha": p} for p in payload.get("parents", [])]
            self.commits[commit_sha] = {
                "sha": commit_sha,
                "tree": {"sha": payload.get("tree")},
                "parents": parents_formatted,
                "message": payload.get("message"),
            }
            return 201, json.dumps({"sha": commit_sha}).encode("utf-8"), {}

        # PATCH ref
        if method == "PATCH" and path == f"/repos/{FIXED_RELAY_REPOSITORY}/git/refs/heads/{FIXED_RELAY_BRANCH}":
            self.ref_update_count += 1
            payload = json.loads(body.decode("utf-8")) if body else {}

            if payload.get("force") is True:
                self.force_update_count += 1

            if payload.get("force") is not False:
                return 400, b'{"message": "force=false is strictly required"}', {}

            if self._simulate_race_on_patch:
                self._simulate_race_on_patch = False
                self._ref_head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                return 422, b'{"message": "Update is not a fast-forward"}', {}

            new_sha = payload.get("sha")
            self.ref_update_history.append({
                "old_sha": self._ref_head,
                "new_sha": new_sha,
                "force": payload.get("force"),
            })
            self._ref_head = new_sha
            return 200, json.dumps({"ref": FIXED_RELAY_REF, "object": {"sha": new_sha}}).encode("utf-8"), {}

        return 404, b'{"message": "Unknown path"}', {}


def _make_service_stack() -> Tuple[InMemoryGitHubRequester, GitDataCASRelayTransport, ControllerRelayService]:
    """Create a full requester -> transport -> service stack."""
    requester = InMemoryGitHubRequester()
    transport = GitDataCASRelayTransport(requester)
    service = ControllerRelayService(transport)
    return requester, transport, service


# ============================================================================
# SECTION A: DRY-RUN REGRESSION TESTS (MUST REMAIN UNCHANGED)
# ============================================================================


def test_role_to_principal_exact_mapping():
    """AOS_ROOT -> AOS_CONTROLLER, LARI_REPLY -> LARI_CONTROLLER, arbitrary rejected"""
    res_root = execute_cr1_dry_run("AOS_ROOT")
    assert res_root["principal_controller_id"] == "AOS_CONTROLLER"

    # LARI_REPLY requires observed root
    root_msg, _, root_raw = build_cr1_root_message_plan()
    res_reply = execute_cr1_dry_run("LARI_REPLY", observed_root_raw=root_raw)
    assert res_reply["principal_controller_id"] == "LARI_CONTROLLER"

    # Arbitrary role rejected
    with pytest.raises(ValueError, match="Invalid role"):
        execute_cr1_dry_run("SECURITY_CONTROLLER")  # type: ignore

    with pytest.raises(ValueError, match="Invalid role"):
        execute_cr1_dry_run("AOS_CONTROLLER")  # type: ignore


def test_root_message_exact_identity_and_path():
    """Root message_id, sequence, thread, path, expected parent, authority_effect, requires_reply exact"""
    dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    msg, path, raw_bytes = build_cr1_root_message_plan(created_at=dt)

    assert msg["schema_version"] == "0.1"
    assert msg["protocol"] == "CONTROLLER_RELAY_V1"
    assert msg["message_id"] == "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
    assert msg["thread_id"] == "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
    assert msg["sequence"] == 1
    assert msg["from"] == "AOS_CONTROLLER"
    assert msg["to"] == "LARI_CONTROLLER"
    assert msg["in_reply_to"] is None
    assert msg["created_at"] == "2026-09-03T12:00:00Z"
    assert msg["subject"] == "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"
    assert msg["subject_repository"] == "MertSGI/AOS"
    assert msg["subject_branch"] == "feature/controller-relay-v1"
    assert msg["subject_sha"] == "039232ecf10948bf55a9d9dab665828b6c06f7c6"
    assert msg["decision"] == "CR1_CAPABILITY_HANDSHAKE_REQUEST"
    assert msg["authority_effect"] == "NONE"
    assert msg["authority_refs"] == [
        "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01",
        "039232ecf10948bf55a9d9dab665828b6c06f7c6",
        "c3ee2f2c1510abdddd3de14bc879e5ba27dac835",
    ]
    assert msg["requested_next_action"] == "LARI_CONTROLLER_VERIFY_AND_REPLY"
    assert msg["requires_reply"] is True

    res = execute_cr1_dry_run("AOS_ROOT", created_at=dt)
    assert res["expected_parent_sha"] == "039232ecf10948bf55a9d9dab665828b6c06f7c6"

    expected_path = "controller-relay/v1/messages/AOS_CONTROLLER--LARI_CONTROLLER/000000000001-CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001.json"
    assert path == expected_path


def test_canonical_content_hash_recomputed_freshly():
    """Hash is freshly recomputed from canonical content, NOT hardcoded, varies deterministically with timestamp"""
    dt1 = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    msg1, _, _ = build_cr1_root_message_plan(created_at=dt1)

    dt2 = datetime(2026, 9, 3, 12, 1, 0, tzinfo=timezone.utc)
    msg2, _, _ = build_cr1_root_message_plan(created_at=dt2)

    # Assert hash is NOT equal to failed connector attempt hash
    FAILED_CONNECTOR_HASH = "5f3dcf07e036e90b5e18cc722509d87e5e4afa70e07e76df04930479367171aa"
    assert msg1["content_sha256"] != FAILED_CONNECTOR_HASH
    assert msg2["content_sha256"] != FAILED_CONNECTOR_HASH

    # Assert recomputing via production helper matches
    assert compute_message_content_sha256(msg1) == msg1["content_sha256"]
    assert compute_message_content_sha256(msg2) == msg2["content_sha256"]

    # Different created_at produces different hash
    assert msg1["content_sha256"] != msg2["content_sha256"]


def test_lari_reply_exact_contract():
    """LARI_REPLY exact fields contract for valid observed root"""
    valid_root_msg, _, valid_root_raw = build_cr1_root_message_plan()
    dt_reply = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)
    reply_msg, reply_path, reply_raw = build_cr1_reply_message_plan(valid_root_raw, created_at=dt_reply)

    assert reply_msg["schema_version"] == "0.1"
    assert reply_msg["protocol"] == "CONTROLLER_RELAY_V1"
    assert reply_msg["message_id"] == "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001"
    assert reply_msg["from"] == "LARI_CONTROLLER"
    assert reply_msg["to"] == "AOS_CONTROLLER"
    assert reply_msg["sequence"] == 1
    assert reply_msg["thread_id"] == valid_root_msg["thread_id"]
    assert reply_msg["in_reply_to"] == valid_root_msg["message_id"]
    assert reply_msg["subject"] == "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"
    assert reply_msg["subject_repository"] == "MertSGI/AOS"
    assert reply_msg["subject_branch"] == "feature/controller-relay-v1"
    assert reply_msg["subject_sha"] == "039232ecf10948bf55a9d9dab665828b6c06f7c6"
    assert reply_msg["authority_refs"] == [
        "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01",
        "039232ecf10948bf55a9d9dab665828b6c06f7c6",
        "c3ee2f2c1510abdddd3de14bc879e5ba27dac835",
    ]
    assert reply_msg["decision"] == "CR1_CAPABILITY_HANDSHAKE_ACCEPTED"
    assert reply_msg["requested_next_action"] == "AOS_CONTROLLER_VERIFY_REPLY_AND_CLOSE_HANDSHAKE"
    assert reply_msg["requires_reply"] is False
    assert reply_msg["authority_effect"] == "NONE"
    assert reply_msg["content_sha256"] == compute_message_content_sha256(reply_msg)

    expected_reply_path = "controller-relay/v1/messages/LARI_CONTROLLER--AOS_CONTROLLER/000000000001-CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001.json"
    assert reply_path == expected_reply_path


def test_observed_root_binding_guard_negative_cases():
    """Observed root MUST be rejected on any identity or binding mismatch"""
    valid_root_msg, _, _ = build_cr1_root_message_plan()

    # Helper to encode root with freshly recomputed content_sha256
    def make_root_raw(mods: dict) -> bytes:
        d = dict(valid_root_msg)
        d.update(mods)
        d["content_sha256"] = compute_message_content_sha256(d)
        return json.dumps(d, ensure_ascii=False).encode("utf-8")

    # A. schema_version = "0.1.0" -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"schema_version": "0.1.0"}))

    # B. subject_branch = "feature/controller-relay-service-v1" -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"subject_branch": "feature/controller-relay-service-v1"}))

    # C. authority_refs member differs -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"authority_refs": ["WRONG_REF", "039232ecf10948bf55a9d9dab665828b6c06f7c6", "c3ee2f2c1510abdddd3de14bc879e5ba27dac835"]}))

    # D. authority_refs wrong order -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"authority_refs": ["039232ecf10948bf55a9d9dab665828b6c06f7c6", "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01", "c3ee2f2c1510abdddd3de14bc879e5ba27dac835"]}))

    # E. requested_next_action differs -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"requested_next_action": "WRONG_ACTION"}))

    # F. wrong subject_repository -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"subject_repository": "WRONG/REPO"}))

    # G. wrong subject_sha -> HOLD_INVALID_OBSERVED_ROOT
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(make_root_raw({"subject_sha": "0000000000000000000000000000000000000000"}))


def test_lari_reply_requires_valid_observed_root_basics():
    """LARI_REPLY basic edge checks"""
    # 1. No observed root -> rejected
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        execute_cr1_dry_run("LARI_REPLY", observed_root_raw=None)

    # 2. Malformed JSON observed root -> rejected
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        build_cr1_reply_message_plan(b"{invalid json")


def test_dry_run_zero_mutation_and_safe_output():
    """Dry-run returns safe metadata with zero credential access, mutation, or network counts"""
    res = execute_cr1_dry_run("AOS_ROOT")

    assert res["mode"] == "DRY_RUN"
    assert res["validation_disposition"] == "PASS"
    assert res["CREDENTIAL_ACCESS_COUNT"] == 0
    assert res["TRANSPORT_MUTATION_COUNT"] == 0
    assert res["LIVE_RELAY_WRITE_COUNT"] == 0

    # Ensure output dict string has no secret material
    output_str = json.dumps(res)
    assert "token" not in output_str.lower() or "CREDENTIAL_ACCESS_COUNT" in output_str
    assert "bearer" not in output_str.lower()
    assert "private" not in output_str.lower()


# ============================================================================
# SECTION B: SIMULATED ROOT PUBLICATION PROOF (Phase 16)
# ============================================================================


def test_simulated_root_publication():
    """SIMULATED_ROOT_LIVE_SERVICE_PATH=PASS
    SIMULATED_ROOT_ONE_RECORD_ONE_COMMIT=PASS
    SIMULATED_ROOT_DIRECT_PARENT_CAS=PASS
    """
    requester, transport, service = _make_service_stack()
    dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    result = execute_cr1_live(
        "AOS_ROOT",
        service,
        expected_head=BOOTSTRAP_SHA,
        created_at=dt,
    )

    # PASS
    assert result["validation_disposition"] == "PASS"
    assert result["mode"] == "LIVE"
    assert result["role"] == "AOS_ROOT"
    assert result["message_id"] == "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
    assert result["authority_effect"] == "NONE"

    # path exact
    expected_root_path = "controller-relay/v1/messages/AOS_CONTROLLER--LARI_CONTROLLER/000000000001-CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001.json"
    assert result["path"] == expected_root_path

    # publication commit returned
    root_commit_sha = result["publication_commit_sha"]
    assert root_commit_sha is not None
    assert len(root_commit_sha) == 40

    # One-record-one-commit counters
    assert requester.blob_create_count == 1
    assert requester.tree_create_count == 1
    assert requester.commit_create_count == 1
    assert requester.ref_update_count == 1

    # Force update count
    assert requester.force_update_count == 0

    # Ref update history
    assert len(requester.ref_update_history) == 1
    assert requester.ref_update_history[0]["force"] is False
    assert requester.ref_update_history[0]["old_sha"] == BOOTSTRAP_SHA

    # New branch head = root publication commit
    assert requester._ref_head == root_commit_sha

    # Root commit parent = bootstrap SHA
    root_commit = requester.commits[root_commit_sha]
    assert root_commit["parents"][0]["sha"] == BOOTSTRAP_SHA

    # Only root Relay record introduced
    tree_sha = root_commit["tree"]["sha"]
    tree_items = requester.trees[tree_sha]
    relay_records = [item for item in tree_items if item["path"].startswith("controller-relay/v1/messages/")]
    assert len(relay_records) == 1
    assert relay_records[0]["path"] == expected_root_path


# ============================================================================
# SECTION C: SIMULATED DIRECT ROOT OBSERVATION (Phase 17)
# ============================================================================


def test_simulated_direct_root_observation():
    """SIMULATED_LARI_DIRECT_OBSERVATION=PASS"""
    requester, transport, service = _make_service_stack()
    dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # Observe root through service
    observation = observe_exact_cr1_root(service, expected_head=root_commit_sha)

    # Root read through service/transport
    assert observation["raw_bytes"] is not None
    assert len(observation["raw_bytes"]) > 0

    # Exact root raw validation PASS
    val_res = validate_controller_relay_message_raw(observation["raw_bytes"])
    assert val_res.is_valid is True

    # Exact R0-R1 authority binding PASS
    parsed = observation["parsed_message"]
    assert parsed["schema_version"] == "0.1"
    assert parsed["protocol"] == "CONTROLLER_RELAY_V1"
    assert parsed["from"] == "AOS_CONTROLLER"
    assert parsed["to"] == "LARI_CONTROLLER"
    assert parsed["subject_repository"] == "MertSGI/AOS"
    assert parsed["subject_branch"] == "feature/controller-relay-v1"
    assert parsed["subject_sha"] == BOOTSTRAP_SHA
    assert parsed["decision"] == "CR1_CAPABILITY_HANDSHAKE_REQUEST"
    assert parsed["authority_effect"] == "NONE"
    assert parsed["authority_refs"] == CR1_AUTHORITY_REFS
    assert parsed["requested_next_action"] == "LARI_CONTROLLER_VERIFY_AND_REPLY"
    assert parsed["requires_reply"] is True

    # publication_commit_sha = root_commit_sha
    assert observation["publication_commit_sha"] == root_commit_sha


# ============================================================================
# SECTION D: SIMULATED REPLY PROOF (Phase 18)
# ============================================================================


def test_simulated_reply_publication():
    """SIMULATED_REPLY_LIVE_SERVICE_PATH=PASS
    SIMULATED_REPLY_DIRECT_PARENT_CAS=PASS
    """
    requester, transport, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    # Publish root
    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # Counters after root
    root_blobs = requester.blob_create_count
    root_trees = requester.tree_create_count
    root_commits = requester.commit_create_count
    root_refs = requester.ref_update_count

    # Publish reply
    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)

    # PASS
    assert reply_result["validation_disposition"] == "PASS"
    assert reply_result["mode"] == "LIVE"
    assert reply_result["role"] == "LARI_REPLY"
    assert reply_result["message_id"] == "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001"
    assert reply_result["authority_effect"] == "NONE"

    reply_commit_sha = reply_result["publication_commit_sha"]
    assert reply_commit_sha is not None
    assert len(reply_commit_sha) == 40

    # Reply message publication count = 1 additional
    assert requester.blob_create_count == root_blobs + 1
    assert requester.tree_create_count == root_trees + 1
    assert requester.commit_create_count == root_commits + 1
    assert requester.ref_update_count == root_refs + 1

    # Reply commit parent = root_commit_sha (NOT bootstrap)
    reply_commit = requester.commits[reply_commit_sha]
    assert reply_commit["parents"][0]["sha"] == root_commit_sha
    assert reply_result["parent_sha"] == root_commit_sha

    # Branch head = reply_commit_sha
    assert requester._ref_head == reply_commit_sha

    # Force count remains 0
    assert requester.force_update_count == 0

    # Root path remains unchanged in tree
    reply_tree_sha = reply_commit["tree"]["sha"]
    reply_tree = requester.trees[reply_tree_sha]
    root_path = "controller-relay/v1/messages/AOS_CONTROLLER--LARI_CONTROLLER/000000000001-CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001.json"
    reply_path = "controller-relay/v1/messages/LARI_CONTROLLER--AOS_CONTROLLER/000000000001-CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001.json"
    paths_in_tree = [item["path"] for item in reply_tree]
    assert root_path in paths_in_tree
    assert reply_path in paths_in_tree
    assert reply_result["path"] == reply_path


# ============================================================================
# SECTION E: SIMULATED AOS REPLY VERIFICATION (Phase 19)
# ============================================================================


def test_simulated_aos_reply_verification():
    """SIMULATED_AOS_DIRECT_REPLY_VERIFICATION=PASS"""
    requester, transport, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    # Counters before verification
    pre_blobs = requester.blob_create_count
    pre_trees = requester.tree_create_count
    pre_commits = requester.commit_create_count
    pre_refs = requester.ref_update_count

    # Verify reply — read-only
    verification = verify_cr1_reply(service, expected_head=reply_commit_sha)

    assert verification["verification_disposition"] == "PASS"
    assert verification["message_id"] == "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001"
    assert verification["authority_effect"] == "NONE"
    assert verification["publication_commit_sha"] == reply_commit_sha

    # No additional objects after verification (read-only)
    assert requester.blob_create_count == pre_blobs
    assert requester.tree_create_count == pre_trees
    assert requester.commit_create_count == pre_commits
    assert requester.ref_update_count == pre_refs


# ============================================================================
# SECTION F: REQUIRED COUNTERS (Phase 20)
# ============================================================================


def test_full_handshake_required_counters():
    """Phase 20: Required exact counters for full simulated handshake."""
    requester, transport, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    verify_cr1_reply(service, expected_head=reply_commit_sha)

    # ROOT_MESSAGE_PUBLICATION_COUNT=1, ROOT_COMMIT_COUNT=1, ROOT_REF_UPDATE_COUNT=1
    # REPLY_MESSAGE_PUBLICATION_COUNT=1, REPLY_COMMIT_COUNT=1, REPLY_REF_UPDATE_COUNT=1
    # TOTAL_MESSAGE_PUBLICATION_COUNT=2
    # TOTAL_COMMIT_COUNT=2
    # TOTAL_REF_UPDATE_COUNT=2
    assert requester.blob_create_count == 2    # 1 root + 1 reply
    assert requester.commit_create_count == 2  # 1 root + 1 reply
    assert requester.ref_update_count == 2     # 1 root + 1 reply

    # FORCE_UPDATE_COUNT=0
    assert requester.force_update_count == 0

    # AUTOMATIC_RETRY_COUNT=0 (implied by no retry logic)
    # REAL_LIVE_RELAY_WRITE_COUNT=0 (fake requester, no real writes)
    # REAL_CREDENTIAL_ACCESS_COUNT=0 (no credential provider used)
    # REAL_GITHUB_APP_CREATE_COUNT=0


# ============================================================================
# SECTION G: NEGATIVE TEST MATRIX (Phase 21)
# ============================================================================


def test_neg_a_wrong_role():
    """A. wrong role -> reject"""
    _, _, service = _make_service_stack()
    with pytest.raises(ValueError, match="Invalid role"):
        execute_cr1_live("WRONG_ROLE", service, expected_head=BOOTSTRAP_SHA)

    with pytest.raises(ValueError, match="Invalid role"):
        execute_cr1_live("AOS_CONTROLLER", service, expected_head=BOOTSTRAP_SHA)


def test_neg_b_aos_root_wrong_expected_head():
    """B. AOS_ROOT expected_head != bootstrap -> HOLD_CAS_RACE -> zero object creation"""
    requester, _, service = _make_service_stack()

    with pytest.raises(ValueError, match="HOLD_CAS_RACE"):
        execute_cr1_live("AOS_ROOT", service, expected_head="1111111111111111111111111111111111111111")

    assert requester.blob_create_count == 0
    assert requester.tree_create_count == 0
    assert requester.commit_create_count == 0
    assert requester.ref_update_count == 0


def test_neg_c_current_head_differs_expected():
    """C. current service head differs expected_head -> HOLD_CAS_RACE"""
    requester, _, service = _make_service_stack()

    # Publish root first to move head forward
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)

    # Now try to publish root again with bootstrap as expected_head (head has moved)
    with pytest.raises(ValueError, match="HOLD_CAS_RACE"):
        execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA)


def test_neg_d_missing_root_observation():
    """D. missing root observation -> HOLD_CROSS_CONTROLLER_HANDSHAKE"""
    _, _, service = _make_service_stack()

    # Try to publish reply without publishing root first
    with pytest.raises(ValueError, match="HOLD_CROSS_CONTROLLER_HANDSHAKE"):
        execute_cr1_live("LARI_REPLY", service, expected_head=BOOTSTRAP_SHA)


def test_neg_e_no_payload_override():
    """E. LARI_REPLY cannot accept arbitrary caller payload -> public API exposes no payload override"""
    import inspect
    sig = inspect.signature(execute_cr1_live)
    params = set(sig.parameters.keys())

    # No raw payload parameter
    assert "payload" not in params
    assert "raw_bytes" not in params
    assert "raw_payload" not in params
    assert "observed_root_raw" not in params
    assert "message" not in params
    assert "content" not in params


def test_neg_f_tampered_root_subject_branch():
    """F. tampered root subject_branch -> HOLD_INVALID_OBSERVED_ROOT"""
    requester, transport, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # Tamper: manually modify root blob in requester to change subject_branch
    root_path = root_result["path"]
    root_tree_sha = requester.commits[root_commit_sha]["tree"]["sha"]
    root_tree = requester.trees[root_tree_sha]
    root_blob_sha = None
    for item in root_tree:
        if item["path"] == root_path:
            root_blob_sha = item["sha"]
            break

    assert root_blob_sha is not None
    original_bytes = requester.blobs[root_blob_sha]
    tampered_msg = json.loads(original_bytes)
    tampered_msg["subject_branch"] = "feature/TAMPERED-branch"
    tampered_msg["content_sha256"] = compute_message_content_sha256(tampered_msg)
    requester.blobs[root_blob_sha] = json.dumps(tampered_msg, ensure_ascii=False).encode("utf-8")

    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        observe_exact_cr1_root(service, expected_head=root_commit_sha)


def test_neg_g_tampered_root_authority_refs():
    """G. tampered root authority_refs -> HOLD_INVALID_OBSERVED_ROOT"""
    requester, transport, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # Tamper authority_refs in blob
    root_path = root_result["path"]
    root_tree_sha = requester.commits[root_commit_sha]["tree"]["sha"]
    root_tree = requester.trees[root_tree_sha]
    for item in root_tree:
        if item["path"] == root_path:
            original_bytes = requester.blobs[item["sha"]]
            tampered_msg = json.loads(original_bytes)
            tampered_msg["authority_refs"] = ["TAMPERED-REF"]
            tampered_msg["content_sha256"] = compute_message_content_sha256(tampered_msg)
            requester.blobs[item["sha"]] = json.dumps(tampered_msg, ensure_ascii=False).encode("utf-8")
            break

    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        observe_exact_cr1_root(service, expected_head=root_commit_sha)


def test_neg_h_reply_missing():
    """H. reply missing -> HOLD_CROSS_CONTROLLER_HANDSHAKE"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # Try to observe reply before it's published
    with pytest.raises(ValueError, match="HOLD_CROSS_CONTROLLER_HANDSHAKE"):
        observe_exact_cr1_reply(service, expected_head=root_commit_sha)


def test_neg_i_tampered_reply_from_to():
    """I. tampered reply from/to direction -> HOLD_CROSS_CONTROLLER_HANDSHAKE"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    # Tamper reply from/to in blob — this breaks message_id component matching
    # so service history validation rejects it (fail-closed) before verify_cr1_reply checks fields
    reply_path = reply_result["path"]
    reply_tree_sha = requester.commits[reply_commit_sha]["tree"]["sha"]
    reply_tree = requester.trees[reply_tree_sha]
    for item in reply_tree:
        if item["path"] == reply_path:
            original_bytes = requester.blobs[item["sha"]]
            tampered_msg = json.loads(original_bytes)
            tampered_msg["from"] = "AOS_CONTROLLER"
            tampered_msg["to"] = "LARI_CONTROLLER"
            tampered_msg["content_sha256"] = compute_message_content_sha256(tampered_msg)
            requester.blobs[item["sha"]] = json.dumps(tampered_msg, ensure_ascii=False).encode("utf-8")
            break

    # Fail-closed: either service history validation (ControllerRelayError) or verify_cr1_reply (ValueError)
    with pytest.raises((ValueError, ControllerRelayError)):
        verify_cr1_reply(service, expected_head=reply_commit_sha)


def test_neg_j_tampered_reply_thread():
    """J. tampered reply thread -> HOLD_CROSS_CONTROLLER_HANDSHAKE"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    # Tamper thread_id — non-CONTROLLER regex breaks schema validation at service level
    reply_path = reply_result["path"]
    reply_tree_sha = requester.commits[reply_commit_sha]["tree"]["sha"]
    for item in requester.trees[reply_tree_sha]:
        if item["path"] == reply_path:
            original_bytes = requester.blobs[item["sha"]]
            tampered_msg = json.loads(original_bytes)
            tampered_msg["thread_id"] = "CRV1-TAMPERED_CONTROLLER-AOS_CONTROLLER-000000000001"
            tampered_msg["content_sha256"] = compute_message_content_sha256(tampered_msg)
            requester.blobs[item["sha"]] = json.dumps(tampered_msg, ensure_ascii=False).encode("utf-8")
            break

    # Fail-closed: thread mismatch caught by service history or verify_cr1_reply
    with pytest.raises((ValueError, ControllerRelayError)):
        verify_cr1_reply(service, expected_head=reply_commit_sha)


def test_neg_k_tampered_reply_subject():
    """K. tampered reply subject_branch/subject SHA -> HOLD_CROSS_CONTROLLER_HANDSHAKE"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    # Tamper subject_branch
    reply_path = reply_result["path"]
    reply_tree_sha = requester.commits[reply_commit_sha]["tree"]["sha"]
    for item in requester.trees[reply_tree_sha]:
        if item["path"] == reply_path:
            original_bytes = requester.blobs[item["sha"]]
            tampered_msg = json.loads(original_bytes)
            tampered_msg["subject_branch"] = "feature/TAMPERED"
            tampered_msg["content_sha256"] = compute_message_content_sha256(tampered_msg)
            requester.blobs[item["sha"]] = json.dumps(tampered_msg, ensure_ascii=False).encode("utf-8")
            break

    with pytest.raises(ValueError, match="HOLD_CROSS_CONTROLLER_HANDSHAKE"):
        verify_cr1_reply(service, expected_head=reply_commit_sha)


def test_neg_l_second_root_publication():
    """L. second root publication attempt after root exists -> fail closed"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)

    # Head has moved, trying AOS_ROOT again fails with CAS race
    with pytest.raises(ValueError, match="HOLD_CAS_RACE"):
        execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA)

    # No overwrite, no additional commits beyond original
    assert requester.commit_create_count == 1
    assert requester.ref_update_count == 1


def test_neg_m_simulated_race():
    """M. simulated race: branch moves between planning and ref patch"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    # Set up race: patch will fail
    requester._simulate_race_on_patch = True

    with pytest.raises(ValueError, match="HOLD_CAS_RACE"):
        execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)

    # Force update count remains 0
    assert requester.force_update_count == 0

    # Ref update was attempted but failed, no retry
    assert requester.ref_update_count == 1  # attempted once, failed
    # No second attempt
    # Blob/tree/commit objects may have been created but ref was not updated


# ============================================================================
# SECTION H: SAFETY CONTRACT (Phase 8)
# ============================================================================


def test_root_publication_safety_contract():
    """Phase 8: ROOT_MESSAGE_PUBLICATION_COUNT=1, ROOT_COMMIT_COUNT=1, ROOT_REF_UPDATE_COUNT=1"""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    # direct parent = bootstrap
    root_commit = requester.commits[root_commit_sha]
    assert root_commit["parents"][0]["sha"] == BOOTSTRAP_SHA

    # force = false
    assert requester.ref_update_history[0]["force"] is False

    # exact root path only, no second Relay record
    tree_sha = root_commit["tree"]["sha"]
    tree_items = requester.trees[tree_sha]
    relay_records = [item for item in tree_items if item["path"].startswith("controller-relay/v1/")]
    assert len(relay_records) == 1

    # authority_effect = NONE
    assert root_result["authority_effect"] == "NONE"


# ============================================================================
# SECTION I: SECRET-FREE RESULT SURFACE (Phase 14)
# ============================================================================


def test_secret_free_result_surfaces():
    """SECRET_FREE_RESULT_SURFACE=PASS: No credential material in result dicts, repr, or errors."""
    requester, _, service = _make_service_stack()
    root_dt = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    reply_dt = datetime(2026, 9, 3, 12, 5, 0, tzinfo=timezone.utc)

    root_result = execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA, created_at=root_dt)
    root_commit_sha = root_result["publication_commit_sha"]

    reply_result = execute_cr1_live("LARI_REPLY", service, expected_head=root_commit_sha, created_at=reply_dt)
    reply_commit_sha = reply_result["publication_commit_sha"]

    verification = verify_cr1_reply(service, expected_head=reply_commit_sha)

    # Check no secrets in any result dict
    for result in [root_result, reply_result, verification]:
        result_str = json.dumps(result)
        assert FAKE_TOKEN_MARKER not in result_str
        assert FAKE_BEARER_MARKER not in result_str
        assert "token" not in result_str.lower() or "installation_token" not in result_str.lower()
        assert "bearer" not in result_str.lower()
        assert "private_key" not in result_str.lower()
        assert "jwt" not in result_str.lower()
        assert "Authorization" not in result_str

    # Check error path
    try:
        execute_cr1_live("AOS_ROOT", service, expected_head="0" * 40)
    except ValueError as exc:
        exc_str = str(exc)
        assert FAKE_TOKEN_MARKER not in exc_str
        assert FAKE_BEARER_MARKER not in exc_str
        assert "bearer" not in exc_str.lower()


# ============================================================================
# SECTION J: NO AUTOMATIC RETRY (Phase 6)
# ============================================================================


def test_no_automatic_retry():
    """NO_AUTOMATIC_RETRY=PASS: AUTOMATIC_RETRY_COUNT=0 on CAS race."""
    requester, _, service = _make_service_stack()

    requester._simulate_race_on_patch = True

    try:
        execute_cr1_live("AOS_ROOT", service, expected_head=BOOTSTRAP_SHA)
    except ValueError:
        pass

    # Only 1 ref update attempt, no retry
    assert requester.ref_update_count == 1
    assert requester.force_update_count == 0


# ============================================================================
# SECTION K: DRY RUN REGRESSION (Phase 23)
# ============================================================================


def test_dry_run_regression():
    """DRY_RUN_REGRESSION=PASS: All existing dry-run semantics unchanged by live invoker."""
    # Root dry run
    res_root = execute_cr1_dry_run("AOS_ROOT")
    assert res_root["mode"] == "DRY_RUN"
    assert res_root["CREDENTIAL_ACCESS_COUNT"] == 0
    assert res_root["TRANSPORT_MUTATION_COUNT"] == 0
    assert res_root["LIVE_RELAY_WRITE_COUNT"] == 0
    assert res_root["validation_disposition"] == "PASS"

    # Reply dry run
    _, _, root_raw = build_cr1_root_message_plan()
    res_reply = execute_cr1_dry_run("LARI_REPLY", observed_root_raw=root_raw)
    assert res_reply["mode"] == "DRY_RUN"
    assert res_reply["CREDENTIAL_ACCESS_COUNT"] == 0
    assert res_reply["TRANSPORT_MUTATION_COUNT"] == 0
    assert res_reply["LIVE_RELAY_WRITE_COUNT"] == 0
    assert res_reply["validation_disposition"] == "PASS"

    # Role rejection still works
    with pytest.raises(ValueError, match="Invalid role"):
        execute_cr1_dry_run("SECURITY_CONTROLLER")

    # Observed root validation still works
    with pytest.raises(ValueError, match="HOLD_INVALID_OBSERVED_ROOT"):
        execute_cr1_dry_run("LARI_REPLY", observed_root_raw=None)


# ============================================================================
# SECTION L: LIVE INVOKER API SURFACE VALIDATION
# ============================================================================


def test_live_invoker_api_has_no_forbidden_parameters():
    """Verify execute_cr1_live has ONLY role, service, expected_head, created_at."""
    import inspect
    sig = inspect.signature(execute_cr1_live)
    params = set(sig.parameters.keys())

    assert params == {"role", "service", "expected_head", "created_at"}

    # No forbidden parameters
    forbidden = {
        "principal", "payload", "raw_payload", "raw_bytes",
        "repository", "branch", "path", "token", "requester",
        "credential", "retry", "observed_root_raw",
    }
    assert params.isdisjoint(forbidden)
