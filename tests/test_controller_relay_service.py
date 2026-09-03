"""Deterministic offline unit tests for AOS ControllerRelayService core.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01
PROVES ZERO NETWORK CALLS.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from aos.controller_relay import (
    ControllerRelayValidationResult,
    compute_message_content_sha256,
    format_message_id,
)
from aos.controller_relay_git_transport import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REPOSITORY,
    RelayRecordProvenance,
)
from aos.controller_relay_service import (
    ControllerPrincipal,
    ControllerRelayService,
    derive_message_path,
    derive_receipt_path,
)

HEAD_SHA_INITIAL = "039232ecf10948bf55a9d9dab665828b6c06f7c6"


class FakeRelayTransport:
    """Deterministic in-memory fake transport for testing ControllerRelayService."""

    def __init__(self, initial_head: str = HEAD_SHA_INITIAL):
        self.head_sha = initial_head
        self.commits: Dict[str, Dict[str, Any]] = {
            initial_head: {"sha": initial_head, "parents": []}
        }
        self.published_records: List[Dict[str, Any]] = []
        self.publish_call_count = 0

        # Simulated failure flags for testing lineage & provenance boundaries
        self.simulate_bootstrap_unreachable = False
        self.simulate_non_linear_lineage = False
        self.simulate_reappeared_path = False
        self.simulate_mutated_path = False
        self.simulate_bootstrap_path_exists = False

    def get_branch_head(self, repository: str, branch: str) -> str:
        assert repository == FIXED_RELAY_REPOSITORY
        assert branch == FIXED_RELAY_BRANCH
        return self.head_sha

    def read_record_bytes(self, repository: str, ref: str, path: str) -> bytes:
        for rec in self.published_records:
            if rec["path"] == path:
                return rec["content_bytes"]
        raise FileNotFoundError(f"Path '{path}' not found in fake transport")

    def get_first_parent_lineage(
        self,
        repository: str,
        head_commit_sha: str,
        bootstrap_sha: str = HEAD_SHA_INITIAL,
    ) -> List[str]:
        if self.simulate_bootstrap_unreachable:
            from aos.controller_relay_git_transport import ControllerRelayTransportError
            raise ControllerRelayTransportError(
                f"HOLD_RELAY_BOOTSTRAP_UNREACHABLE: Trusted bootstrap SHA '{bootstrap_sha}' is unreachable from HEAD '{head_commit_sha}'"
            )
        if self.simulate_non_linear_lineage:
            from aos.controller_relay_git_transport import ControllerRelayTransportError
            raise ControllerRelayTransportError(
                f"HOLD_INVALID_RELAY_HISTORY: Non-linear lineage commit with multiple parents encountered at '{head_commit_sha}'"
            )

        curr_sha = head_commit_sha
        lineage: List[str] = []
        visited = set()

        while curr_sha:
            if curr_sha in visited:
                from aos.controller_relay_git_transport import ControllerRelayTransportError
                raise ControllerRelayTransportError("HOLD_INVALID_RELAY_HISTORY: Cycle in lineage")
            visited.add(curr_sha)
            lineage.append(curr_sha)

            if curr_sha == bootstrap_sha:
                return list(reversed(lineage))

            c_info = self.commits.get(curr_sha)
            if not c_info or not c_info.get("parents"):
                from aos.controller_relay_git_transport import ControllerRelayTransportError
                raise ControllerRelayTransportError(
                    f"HOLD_RELAY_BOOTSTRAP_UNREACHABLE: Trusted bootstrap SHA '{bootstrap_sha}' is unreachable from HEAD '{head_commit_sha}'"
                )

            parents = c_info["parents"]
            if len(parents) > 1:
                from aos.controller_relay_git_transport import ControllerRelayTransportError
                raise ControllerRelayTransportError("HOLD_INVALID_RELAY_HISTORY: Non-linear lineage")

            curr_sha = parents[0]

        return list(reversed(lineage))

    def list_records_under_prefix(self, repository: str, ref: str, prefix: str) -> List[Tuple[str, bytes]]:
        res = []
        for rec in self.published_records:
            if rec["path"].startswith(prefix):
                res.append((rec["path"], rec["content_bytes"]))
        return res

    def list_record_provenance_under_prefix(self, repository: str, ref: str, prefix: str) -> List[RelayRecordProvenance]:
        if self.simulate_reappeared_path:
            from aos.controller_relay_git_transport import ControllerRelayTransportError
            raise ControllerRelayTransportError("HOLD_RELAY_RECORD_REAPPEARED: Relay record path reappeared in ancestry")
        if self.simulate_mutated_path:
            from aos.controller_relay_git_transport import ControllerRelayTransportError
            raise ControllerRelayTransportError("HOLD_RELAY_RECORD_MUTATED: Relay record content mutated across history")
        if self.simulate_bootstrap_path_exists:
            from aos.controller_relay_git_transport import ControllerRelayTransportError
            raise ControllerRelayTransportError("HOLD_RECORD_PROVENANCE_UNVERIFIABLE: Record already exists at trusted bootstrap")

        # Verify lineage first
        lineage = self.get_first_parent_lineage(repository, ref, HEAD_SHA_INITIAL)
        ordinal_map = {sha: idx for idx, sha in enumerate(lineage)}

        res = []
        for rec in self.published_records:
            if rec["path"].startswith(prefix):
                pub_sha = rec.get("commit_sha", HEAD_SHA_INITIAL)
                pub_ord = ordinal_map.get(pub_sha, 0)
                res.append(RelayRecordProvenance(rec["path"], rec["content_bytes"], pub_sha, pub_ord))
        return res

    def publish_record(
        self,
        repository: str,
        branch: str,
        path: str,
        content_bytes: bytes,
        expected_head: str,
        record_type: str = "record",
    ) -> ControllerRelayValidationResult:
        assert repository == FIXED_RELAY_REPOSITORY
        assert branch == FIXED_RELAY_BRANCH
        self.publish_call_count += 1
        if expected_head != self.head_sha:
            return ControllerRelayValidationResult(
                False, "HOLD_CAS_RACE", [f"Expected head mismatch: got {expected_head}, actual {self.head_sha}"]
            )
        new_commit_sha = f"{self.publish_call_count:010x}" + "a" * 30
        self.commits[new_commit_sha] = {"sha": new_commit_sha, "parents": [self.head_sha]}
        self.published_records.append({
            "path": path,
            "content_bytes": content_bytes,
            "record_type": record_type,
            "commit_sha": new_commit_sha,
        })
        self.head_sha = new_commit_sha
        return ControllerRelayValidationResult(
            True, "PASS", details={"commit_sha": new_commit_sha, "path": path, "expected_head": expected_head}
        )


def make_valid_message_dict(
    from_c: str = "LARI_CONTROLLER",
    to_c: str = "AOS_CONTROLLER",
    seq: int = 1,
    message_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    requires_reply: bool = False,
    decision: str = "PROCEED_TO_EXECUTION",
    authority_effect: str = "NONE",
) -> Dict[str, Any]:
    msg_id = message_id or format_message_id(from_c, to_c, seq)
    thr_id = thread_id or (msg_id if in_reply_to is None else "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001")
    msg: Dict[str, Any] = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": msg_id,
        "thread_id": thr_id,
        "sequence": seq,
        "from": from_c,
        "to": to_c,
        "in_reply_to": in_reply_to,
        "created_at": "2026-09-03T12:00:00Z",
        "subject": "Preflight evaluation request",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "feature/controller-relay-v1",
        "subject_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
        "decision": decision,
        "authority_effect": authority_effect,
        "authority_refs": ["docs/project-control/STATE.json#L1"],
        "requested_next_action": "Evaluate execution readiness",
        "requires_reply": requires_reply,
    }
    msg["content_sha256"] = compute_message_content_sha256(msg)
    return msg


def make_valid_receipt_dict(
    msg: Dict[str, Any],
    actor: str = "AOS_CONTROLLER",
    event: str = "OBSERVED",
    message_commit_sha: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": msg["message_id"],
        "message_content_sha256": msg["content_sha256"],
        "message_commit_sha": message_commit_sha or "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
        "actor": actor,
        "event": event,
        "created_at": "2026-09-03T12:01:00Z",
    }


def test_controller_principal_validation():
    p = ControllerPrincipal("LARI_CONTROLLER")
    assert p.controller_id == "LARI_CONTROLLER"
    assert repr(p) == "ControllerPrincipal(controller_id='LARI_CONTROLLER')"

    with pytest.raises(ValueError, match="Invalid controller_id"):
        ControllerPrincipal("invalid-id")


def test_path_derivation():
    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    path = derive_message_path(msg)
    assert path == f"controller-relay/v1/messages/LARI_CONTROLLER--AOS_CONTROLLER/000000000001-{msg['message_id']}.json"

    rcpt = make_valid_receipt_dict(msg, "AOS_CONTROLLER", "OBSERVED")
    r_path = derive_receipt_path(rcpt)
    assert r_path == f"controller-relay/v1/receipts/{msg['message_id']}/AOS_CONTROLLER-OBSERVED.json"


def test_publish_valid_message():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    raw_bytes = json.dumps(msg).encode("utf-8")

    res = service.publish_message(raw_bytes, HEAD_SHA_INITIAL, principal)
    assert res.is_valid is True
    assert res.disposition == "PASS"
    assert transport.publish_call_count == 1
    assert len(transport.published_records) == 1


def test_principal_mismatch_rejects_before_transport():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    raw_bytes = json.dumps(msg).encode("utf-8")

    # Principal is AOS_CONTROLLER, but message is from LARI_CONTROLLER
    wrong_principal = ControllerPrincipal("AOS_CONTROLLER")
    res = service.publish_message(raw_bytes, HEAD_SHA_INITIAL, wrong_principal)

    assert res.is_valid is False
    assert res.disposition == "FAIL"
    assert "Authentication mismatch" in res.errors[0]
    assert transport.publish_call_count == 0


def test_valid_receipt_and_matching_principal():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_lari = ControllerPrincipal("LARI_CONTROLLER")

    # First publish message from LARI
    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    raw_msg_bytes = json.dumps(msg).encode("utf-8")
    m_res = service.publish_message(raw_msg_bytes, HEAD_SHA_INITIAL, principal_lari)
    assert m_res.is_valid is True

    head_after_msg = service.get_head()

    # Publish receipt from AOS_CONTROLLER
    principal_aos = ControllerPrincipal("AOS_CONTROLLER")
    rcpt = make_valid_receipt_dict(msg, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m_res.details["commit_sha"])
    raw_rcpt_bytes = json.dumps(rcpt).encode("utf-8")

    r_res = service.publish_receipt(raw_rcpt_bytes, head_after_msg, principal_aos)
    assert r_res.errors == []
    assert r_res.is_valid is True
    assert r_res.disposition == "PASS"
    assert transport.publish_call_count == 2


def test_receipt_wrong_principal_rejects_before_transport():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    raw_msg_bytes = json.dumps(msg).encode("utf-8")
    service.publish_message(raw_msg_bytes, HEAD_SHA_INITIAL, ControllerPrincipal("LARI_CONTROLLER"))

    rcpt = make_valid_receipt_dict(msg, "AOS_CONTROLLER", "OBSERVED")
    raw_rcpt_bytes = json.dumps(rcpt).encode("utf-8")

    # Principal is LARI_CONTROLLER but receipt actor is AOS_CONTROLLER
    wrong_principal = ControllerPrincipal("LARI_CONTROLLER")
    res = service.publish_receipt(raw_rcpt_bytes, service.get_head(), wrong_principal)

    assert res.is_valid is False
    assert res.disposition == "FAIL"
    assert "Authentication mismatch" in res.errors[0]
    assert transport.publish_call_count == 1  # only initial message published


def test_invalid_raw_message_rejects_before_transport():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    # Invalid JSON
    res = service.publish_message(b"{invalid json", HEAD_SHA_INITIAL, principal)
    assert res.is_valid is False
    assert transport.publish_call_count == 0

    # UTF-8 BOM
    bom_msg = b"\xef\xbb\xbf" + json.dumps(make_valid_message_dict()).encode("utf-8")
    res_bom = service.publish_message(bom_msg, HEAD_SHA_INITIAL, principal)
    assert res_bom.is_valid is False
    assert transport.publish_call_count == 0


def test_authority_effect_non_none_rejects():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1, authority_effect="GRANT")
    raw_bytes = json.dumps(msg).encode("utf-8")

    res = service.publish_message(raw_bytes, HEAD_SHA_INITIAL, principal)
    assert res.is_valid is False
    assert any("authority_effect" in err for err in res.errors)
    assert transport.publish_call_count == 0


def test_secret_containing_record_rejects():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    # Add secret pattern
    msg["requested_next_action"] = "Bearer ghp_123456789012345678901234567890123456"
    msg["content_sha256"] = compute_message_content_sha256(msg)
    raw_bytes = json.dumps(msg).encode("utf-8")

    res = service.publish_message(raw_bytes, HEAD_SHA_INITIAL, principal)
    assert res.is_valid is False
    assert any("secret" in err.lower() or "credential" in err.lower() for err in res.errors)
    assert transport.publish_call_count == 0


def test_sequence_validator_invoked():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    # Publish seq 1
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    service.publish_message(json.dumps(msg1).encode("utf-8"), HEAD_SHA_INITIAL, principal)

    head1 = service.get_head()

    # Attempt to publish seq 3 (gap)
    msg3 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 3)
    res = service.publish_message(json.dumps(msg3).encode("utf-8"), head1, principal)

    assert res.is_valid is False
    assert any("Sequence gap" in err for err in res.errors)
    assert transport.publish_call_count == 1


def test_thread_validator_invoked():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal = ControllerPrincipal("LARI_CONTROLLER")

    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    msg["in_reply_to"] = "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000099"  # Unknown prior reply
    msg["content_sha256"] = compute_message_content_sha256(msg)

    res = service.publish_message(json.dumps(msg).encode("utf-8"), HEAD_SHA_INITIAL, principal)
    assert res.is_valid is False
    assert any("unknown in_reply_to" in err for err in res.errors)
    assert transport.publish_call_count == 0


def test_fixed_repository_and_branch_invariants():
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    assert service.repository == "MertSGI/AOS"
    assert service.branch == "control/controller-relay"


# --- R1 HARDENING PROOF SUITES ---

def test_invalid_history_fail_closed():
    """INVALID_HISTORY_FAIL_CLOSED tests."""
    principal = ControllerPrincipal("LARI_CONTROLLER")

    # 1. Malformed JSON message in history
    t1 = FakeRelayTransport()
    t1.published_records.append({
        "path": "controller-relay/v1/messages/LARI--AOS/0001.json",
        "content_bytes": b"{malformed json",
        "commit_sha": "0000000001aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    })
    s1 = ControllerRelayService(t1)
    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 2)
    res1 = s1.publish_message(json.dumps(msg).encode("utf-8"), t1.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH), principal)
    assert res1.is_valid is False
    assert res1.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert t1.publish_call_count == 0

    # 2. UTF-8 BOM historical message
    t2 = FakeRelayTransport()
    valid_msg_dict = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    bom_bytes = b"\xef\xbb\xbf" + json.dumps(valid_msg_dict).encode("utf-8")
    t2.published_records.append({
        "path": "controller-relay/v1/messages/LARI--AOS/0001.json",
        "content_bytes": bom_bytes,
        "commit_sha": "0000000001aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    })
    s2 = ControllerRelayService(t2)
    res2 = s2.publish_message(json.dumps(msg).encode("utf-8"), t2.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH), principal)
    assert res2.is_valid is False
    assert res2.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert t2.publish_call_count == 0

    # 3. Duplicate-key historical message
    t3 = FakeRelayTransport()
    dup_key_bytes = b'{"schema_version": "0.1.0", "schema_version": "0.1.0"}'
    t3.published_records.append({
        "path": "controller-relay/v1/messages/LARI--AOS/0001.json",
        "content_bytes": dup_key_bytes,
        "commit_sha": "0000000001aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    })
    s3 = ControllerRelayService(t3)
    res3 = s3.publish_message(json.dumps(msg).encode("utf-8"), t3.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH), principal)
    assert res3.is_valid is False
    assert res3.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert t3.publish_call_count == 0

    # 4. Invalid historical receipt
    t4 = FakeRelayTransport()
    t4.published_records.append({
        "path": "controller-relay/v1/receipts/CRV1-1/AOS-OBSERVED.json",
        "content_bytes": b"{invalid json receipt",
        "commit_sha": "0000000001aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    })
    s4 = ControllerRelayService(t4)
    res4 = s4.publish_message(json.dumps(msg).encode("utf-8"), t4.get_branch_head(FIXED_RELAY_REPOSITORY, FIXED_RELAY_BRANCH), principal)
    assert res4.is_valid is False
    assert res4.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert t4.publish_call_count == 0


def test_receipt_message_commit_binding():
    """RECEIPT_MESSAGE_COMMIT_BINDING tests."""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # Publish message M1
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    m1_res = service.publish_message(json.dumps(msg1).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    assert m1_res.is_valid is True
    actual_pub_sha = m1_res.details["commit_sha"]

    head1 = service.get_head()

    # 1. Wrong 40-char message_commit_sha -> FAIL CLOSED (HOLD_RECEIPT_MESSAGE_COMMIT_MISMATCH)
    wrong_sha = "9999999999aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    rcpt_wrong = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=wrong_sha)
    res_wrong = service.publish_receipt(json.dumps(rcpt_wrong).encode("utf-8"), head1, principal_a)

    assert res_wrong.is_valid is False
    assert res_wrong.disposition == "HOLD_RECEIPT_MESSAGE_COMMIT_MISMATCH"
    assert transport.publish_call_count == 1  # Only M1 published

    # 2. Exact true publication commit SHA -> PASS
    rcpt_exact = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=actual_pub_sha)
    res_exact = service.publish_receipt(json.dumps(rcpt_exact).encode("utf-8"), head1, principal_a)

    assert res_exact.is_valid is True
    assert res_exact.disposition == "PASS"
    assert transport.publish_call_count == 2


def test_requires_reply_true_reply_guard():
    """REQUIRES_REPLY_TRUE_REPLY_GUARD tests."""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # 1. Publish root message M1 with requires_reply=True
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1, requires_reply=True)
    m1_res = service.publish_message(json.dumps(msg1).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    m1_sha = m1_res.details["commit_sha"]

    # Publish receipts OBSERVED, VERIFIED, ACKNOWLEDGED for M1
    h = service.get_head()
    r_obs = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(r_obs).encode("utf-8"), h, principal_a)

    h = service.get_head()
    r_ver = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "VERIFIED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(r_ver).encode("utf-8"), h, principal_a)

    h = service.get_head()
    r_ack = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "ACKNOWLEDGED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(r_ack).encode("utf-8"), h, principal_a)

    h = service.get_head()
    # Attempt CONSUMED when NO actual outbound reply exists -> FAIL CLOSED
    r_con = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "CONSUMED", message_commit_sha=m1_sha)
    con_res_fail = service.publish_receipt(json.dumps(r_con).encode("utf-8"), h, principal_a)
    assert con_res_fail.is_valid is False
    assert any("requiring reply, but no valid outbound reply decision exists" in err for err in con_res_fail.errors)

    # Now publish real valid inverted reply R2 from AOS_CONTROLLER to LARI_CONTROLLER
    msg2 = make_valid_message_dict(
        from_c="AOS_CONTROLLER",
        to_c="LARI_CONTROLLER",
        seq=1,
        in_reply_to=msg1["message_id"],
        thread_id=msg1["thread_id"],
        decision="PROCEED_TO_EXECUTION",
    )
    m2_res = service.publish_message(json.dumps(msg2).encode("utf-8"), h, principal_a)
    assert m2_res.is_valid is True

    h = service.get_head()
    # Attempt CONSUMED again now that valid inverted reply exists -> PASS
    con_res_pass = service.publish_receipt(json.dumps(r_con).encode("utf-8"), h, principal_a)
    assert con_res_pass.is_valid is True
    assert con_res_pass.disposition == "PASS"


# --- SECTION 15 FOUNDATION R2 REQUIRED PROOF TESTS ---

def test_full_historical_receipt_integrity():
    """FULL_HISTORICAL_RECEIPT_INTEGRITY=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # Publish message M1
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    m1_res = service.publish_message(json.dumps(msg1).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    assert m1_res.is_valid is True
    m1_sha = m1_res.details["commit_sha"]

    # Publish receipt OBSERVED for M1
    rcpt_obs = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m1_sha)
    r1_res = service.publish_receipt(json.dumps(rcpt_obs).encode("utf-8"), service.get_head(), principal_a)
    assert r1_res.is_valid is True

    # Validate global history
    msg_map, rcpt_entries, hist_res = service._validate_existing_relay_history(service.get_head())
    assert hist_res.is_valid is True
    assert hist_res.disposition == "PASS"
    assert len(msg_map) == 1
    assert len(rcpt_entries) == 1


def test_historical_receipt_unknown_message_fails():
    """HISTORICAL_RECEIPT_UNKNOWN_MESSAGE_FAILS=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # Inject receipt referencing non-existent message into history
    orphan_rcpt = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000999",
        "message_content_sha256": "a" * 64,
        "message_commit_sha": "b" * 40,
        "actor": "AOS_CONTROLLER",
        "event": "OBSERVED",
        "created_at": "2026-09-03T12:00:00Z",
    }
    transport.published_records.append({
        "path": "controller-relay/v1/receipts/CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000999/AOS_CONTROLLER-OBSERVED.json",
        "content_bytes": json.dumps(orphan_rcpt).encode("utf-8"),
        "commit_sha": "c" * 40,
    })

    # Attempt to publish a new valid message -> blocked by bad history!
    new_msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    res = service.publish_message(json.dumps(new_msg).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    assert res.is_valid is False
    assert res.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert res.details.get("GIT_OBJECT_CREATE_COUNT", 0) == 0
    assert res.details.get("REF_UPDATE_COUNT", 0) == 0
    assert transport.publish_call_count == 0


def test_historical_receipt_content_hash_binding():
    """HISTORICAL_RECEIPT_CONTENT_HASH_BINDING=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # Publish M1
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    m1_res = service.publish_message(json.dumps(msg1).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    m1_sha = m1_res.details["commit_sha"]

    # Receipt with wrong content_sha256
    rcpt_bad_hash = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m1_sha)
    rcpt_bad_hash["message_content_sha256"] = "f" * 64

    res = service.publish_receipt(json.dumps(rcpt_bad_hash).encode("utf-8"), service.get_head(), principal_a)
    assert res.is_valid is False
    assert res.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert transport.publish_call_count == 1


def test_historical_receipt_actual_git_order():
    """HISTORICAL_RECEIPT_ACTUAL_GIT_ORDER=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")

    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    m1_bytes = json.dumps(msg1).encode("utf-8")
    m1_res = service.publish_message(m1_bytes, HEAD_SHA_INITIAL, principal_l)
    m1_sha = m1_res.details["commit_sha"]
    h1 = service.get_head()

    # Manually inject ACKNOWLEDGED before OBSERVED into transport commits
    rcpt_ack = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "ACKNOWLEDGED", message_commit_sha=m1_sha)
    ack_bytes = json.dumps(rcpt_ack).encode("utf-8")
    ack_sha = "1111111111111111111111111111111111111111"
    transport.commits[ack_sha] = {"sha": ack_sha, "parents": [h1]}
    transport.published_records.append({
        "path": f"controller-relay/v1/receipts/{msg1['message_id']}/AOS_CONTROLLER-ACKNOWLEDGED.json",
        "content_bytes": ack_bytes,
        "commit_sha": ack_sha,
    })

    rcpt_obs = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m1_sha)
    obs_bytes = json.dumps(rcpt_obs).encode("utf-8")
    obs_sha = "2222222222222222222222222222222222222222"
    transport.commits[obs_sha] = {"sha": obs_sha, "parents": [ack_sha]}
    transport.published_records.append({
        "path": f"controller-relay/v1/receipts/{msg1['message_id']}/AOS_CONTROLLER-OBSERVED.json",
        "content_bytes": obs_bytes,
        "commit_sha": obs_sha,
    })
    transport.head_sha = obs_sha

    # Verify history validation fails because ACKNOWLEDGED came first in Git commit order!
    _, _, val_res = service._validate_existing_relay_history(obs_sha)
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_INVALID_RELAY_HISTORY"


def test_reply_after_consumed_does_not_retroactively_pass():
    """REPLY_AFTER_CONSUMED_DOES_NOT_RETROACTIVELY_PASS=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # M1 with requires_reply=True
    msg1 = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1, requires_reply=True)
    m1_bytes = json.dumps(msg1).encode("utf-8")
    m1_res = service.publish_message(m1_bytes, HEAD_SHA_INITIAL, principal_l)
    m1_sha = m1_res.details["commit_sha"]

    h1 = service.get_head()
    rcpt_obs = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "OBSERVED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(rcpt_obs).encode("utf-8"), h1, principal_a)

    h2 = service.get_head()
    rcpt_ver = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "VERIFIED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(rcpt_ver).encode("utf-8"), h2, principal_a)

    h3 = service.get_head()
    rcpt_ack = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "ACKNOWLEDGED", message_commit_sha=m1_sha)
    service.publish_receipt(json.dumps(rcpt_ack).encode("utf-8"), h3, principal_a)

    h4 = service.get_head()
    # Manually inject CONSUMED at ordinal 5 before any reply exists
    rcpt_con = make_valid_receipt_dict(msg1, "AOS_CONTROLLER", "CONSUMED", message_commit_sha=m1_sha)
    con_bytes = json.dumps(rcpt_con).encode("utf-8")
    con_sha = "3333333333333333333333333333333333333333"
    transport.commits[con_sha] = {"sha": con_sha, "parents": [h4]}
    transport.published_records.append({
        "path": f"controller-relay/v1/receipts/{msg1['message_id']}/AOS_CONTROLLER-CONSUMED.json",
        "content_bytes": con_bytes,
        "commit_sha": con_sha,
    })
    transport.head_sha = con_sha

    # Manually inject reply at ordinal 6 AFTER CONSUMED
    reply_msg = make_valid_message_dict(
        from_c="AOS_CONTROLLER",
        to_c="LARI_CONTROLLER",
        seq=1,
        in_reply_to=msg1["message_id"],
        thread_id=msg1["thread_id"],
    )
    rep_bytes = json.dumps(reply_msg).encode("utf-8")
    rep_sha = "4444444444444444444444444444444444444444"
    transport.commits[rep_sha] = {"sha": rep_sha, "parents": [con_sha]}
    transport.published_records.append({
        "path": f"controller-relay/v1/messages/AOS_CONTROLLER--LARI_CONTROLLER/000000000001-{reply_msg['message_id']}.json",
        "content_bytes": rep_bytes,
        "commit_sha": rep_sha,
    })
    transport.head_sha = rep_sha

    # History validation MUST FAIL because reply came AFTER CONSUMED!
    _, _, val_res = service._validate_existing_relay_history(rep_sha)
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_INVALID_RELAY_HISTORY"


def test_unrelated_invalid_receipt_blocks_publication():
    """UNRELATED_INVALID_RECEIPT_BLOCKS_MESSAGE_PUBLICATION=PASS & UNRELATED_INVALID_RECEIPT_BLOCKS_RECEIPT_PUBLICATION=PASS"""
    transport = FakeRelayTransport()
    service = ControllerRelayService(transport)
    principal_l = ControllerPrincipal("LARI_CONTROLLER")
    principal_a = ControllerPrincipal("AOS_CONTROLLER")

    # Inject an invalid receipt for an unrelated message
    transport.published_records.append({
        "path": "controller-relay/v1/receipts/CRV1-X-Y-000000000001/X-OBSERVED.json",
        "content_bytes": b"{bad json",
        "commit_sha": "5555555555555555555555555555555555555555",
    })

    # 1. Blocks publish_message
    msg = make_valid_message_dict("LARI_CONTROLLER", "AOS_CONTROLLER", 1)
    res_m = service.publish_message(json.dumps(msg).encode("utf-8"), HEAD_SHA_INITIAL, principal_l)
    assert res_m.is_valid is False
    assert res_m.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert transport.publish_call_count == 0

    # 2. Blocks publish_receipt
    rcpt = make_valid_receipt_dict(msg, "AOS_CONTROLLER", "OBSERVED")
    res_r = service.publish_receipt(json.dumps(rcpt).encode("utf-8"), HEAD_SHA_INITIAL, principal_a)
    assert res_r.is_valid is False
    assert res_r.disposition == "HOLD_INVALID_RELAY_HISTORY"
    assert transport.publish_call_count == 0


def test_delete_readd_provenance_detection():
    """DELETE_READD_PROVENANCE_DETECTION=PASS"""
    transport = FakeRelayTransport()
    transport.simulate_reappeared_path = True
    service = ControllerRelayService(transport)

    _, _, val_res = service._validate_existing_relay_history(HEAD_SHA_INITIAL)
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_RELAY_RECORD_REAPPEARED"


def test_trusted_relay_bootstrap_boundary():
    """TRUSTED_RELAY_BOOTSTRAP_BOUNDARY=PASS"""
    transport = FakeRelayTransport()
    transport.simulate_bootstrap_path_exists = True
    service = ControllerRelayService(transport)

    _, _, val_res = service._validate_existing_relay_history(HEAD_SHA_INITIAL)
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_RECORD_PROVENANCE_UNVERIFIABLE"


def test_bootstrap_unreachable_fails():
    """BOOTSTRAP_UNREACHABLE_FAILS=PASS"""
    transport = FakeRelayTransport()
    transport.simulate_bootstrap_unreachable = True
    service = ControllerRelayService(transport)

    _, _, val_res = service._validate_existing_relay_history("unreachable_head")
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_RELAY_BOOTSTRAP_UNREACHABLE"


def test_non_linear_lineage_fails():
    """NON_LINEAR_LINEAGE_FAILS=PASS"""
    transport = FakeRelayTransport()
    transport.simulate_non_linear_lineage = True
    service = ControllerRelayService(transport)

    _, _, val_res = service._validate_existing_relay_history(HEAD_SHA_INITIAL)
    assert val_res.is_valid is False
    assert val_res.disposition == "HOLD_INVALID_RELAY_HISTORY"

