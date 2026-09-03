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
from aos.controller_relay_service import (
    FIXED_RELAY_BRANCH,
    FIXED_RELAY_REPOSITORY,
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
        self.messages: List[Dict[str, Any]] = []
        self.receipts: List[Dict[str, Any]] = []
        self.published_records: List[Dict[str, Any]] = []
        self.publish_call_count = 0

    def get_branch_head(self, repository: str, branch: str) -> str:
        assert repository == FIXED_RELAY_REPOSITORY
        assert branch == FIXED_RELAY_BRANCH
        return self.head_sha

    def read_record_bytes(self, repository: str, ref: str, path: str) -> bytes:
        for rec in self.published_records:
            if rec["path"] == path:
                return rec["content_bytes"]
        raise FileNotFoundError(f"Path '{path}' not found in fake transport")

    def list_records_under_prefix(self, repository: str, ref: str, prefix: str) -> List[Tuple[str, bytes]]:
        res = []
        for rec in self.published_records:
            if rec["path"].startswith(prefix):
                res.append((rec["path"], rec["content_bytes"]))
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
        new_commit_sha = f"commit-{self.publish_call_count:04d}-" + "a" * 30
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
) -> Dict[str, Any]:
    return {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": msg["message_id"],
        "message_content_sha256": msg["content_sha256"],
        "message_commit_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
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
    rcpt = make_valid_receipt_dict(msg, "AOS_CONTROLLER", "OBSERVED")
    raw_rcpt_bytes = json.dumps(rcpt).encode("utf-8")

    r_res = service.publish_receipt(raw_rcpt_bytes, head_after_msg, principal_aos)
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
