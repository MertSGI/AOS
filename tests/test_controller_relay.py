"""Tests for AOS Controller Relay V1 protocol, schemas, validation engine, state machine, and raw parsers."""

import json
from pathlib import Path
import pytest

from aos.controller_relay import (
    RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY,
    canonicalize_message_content,
    compute_message_content_sha256,
    detect_relay_conflicts_and_replays,
    format_message_id,
    parse_message_id,
    scan_for_prohibited_secrets,
    validate_channel_sequence_history,
    validate_controller_relay_message,
    validate_controller_relay_message_raw,
    validate_controller_relay_receipt,
    validate_controller_relay_receipt_raw,
    validate_receipt_lifecycle,
    validate_thread_history,
    verify_message_content_sha256,
)
from aos.validate import validate_document, validate_file

PROTOCOL_MD_PATH = Path(__file__).parent.parent / "docs" / "controller-relay" / "PROTOCOL.md"


def make_valid_message(
    from_c: str = "AOS_CONTROLLER",
    to_c: str = "LARI_CONTROLLER",
    sequence: int = 1,
    message_id: str | None = None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    requires_reply: bool = True,
    authority_effect: str = "NONE",
    supersedes_message_id: str | None = None,
    decision: str = "PROCEED_TO_EXECUTION",
) -> dict:
    msg_id = message_id or format_message_id(from_c, to_c, sequence)
    thr_id = thread_id or (msg_id if (in_reply_to is None and supersedes_message_id is None) else "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001")

    base_msg = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": msg_id,
        "thread_id": thr_id,
        "sequence": sequence,
        "from": from_c,
        "to": to_c,
        "in_reply_to": in_reply_to,
        "created_at": "2026-09-03T08:00:00Z",
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
    if supersedes_message_id:
        base_msg["supersedes_message_id"] = supersedes_message_id

    # Compute valid content hash
    base_msg["content_sha256"] = compute_message_content_sha256(base_msg)
    return base_msg


def make_valid_receipt(
    message_id: str = "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
    message_content_sha256: str = "0000000000000000000000000000000000000000000000000000000000000000",
    actor: str = "LARI_CONTROLLER",
    event: str = "OBSERVED",
) -> dict:
    return {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
        "message_id": message_id,
        "message_content_sha256": message_content_sha256,
        "message_commit_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
        "actor": actor,
        "event": event,
        "created_at": "2026-09-03T08:05:00Z",
    }


# ==============================================================================
# PASS TESTS
# ==============================================================================

class TestControllerRelayPass:
    def test_valid_root_message(self):
        msg = make_valid_message()
        res = validate_controller_relay_message(msg)
        assert res.is_valid is True
        assert res.disposition == "PASS"
        assert len(res.errors) == 0

    def test_valid_reply(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        reply = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
        )
        res = validate_thread_history([root, reply])
        assert res.is_valid is True

    def test_independent_two_direction_sequences(self):
        m1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        m2 = make_valid_message(from_c="LARI_CONTROLLER", to_c="AOS_CONTROLLER", sequence=1)
        m3 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=2)
        res = validate_channel_sequence_history([m1, m2, m3])
        assert res.is_valid is True

    def test_valid_canonical_content_hash(self):
        msg = make_valid_message()
        assert verify_message_content_sha256(msg) is True

    def test_valid_full_receipt_lifecycle(self):
        msg = make_valid_message(requires_reply=True)
        reply = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=msg["message_id"],
            in_reply_to=msg["message_id"],
        )
        rcpts = [
            make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "OBSERVED"),
            make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "VERIFIED"),
            make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "ACKNOWLEDGED"),
            make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "CONSUMED"),
        ]
        res = validate_receipt_lifecycle(msg, rcpts, outbound_replies=[reply])
        assert res.is_valid is True

    def test_valid_generic_security_controller_identity(self):
        msg = make_valid_message(from_c="SECURITY_CONTROLLER", to_c="RELEASE_CONTROLLER", sequence=1)
        res = validate_controller_relay_message(msg)
        assert res.is_valid is True

    def test_valid_supersession(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        superseding = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=2,
            thread_id=root["message_id"],
            in_reply_to=None,
            supersedes_message_id=root["message_id"],
        )
        res = validate_thread_history([root, superseding])
        assert res.is_valid is True

    def test_valid_transport_consumption_after_reply(self):
        root = make_valid_message(requires_reply=True)
        reply = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
        )
        rcpt = make_valid_receipt(root["message_id"], root["content_sha256"], "LARI_CONTROLLER", "CONSUMED")

        rcpt_res = validate_receipt_lifecycle(
            root,
            [
                make_valid_receipt(root["message_id"], root["content_sha256"], "LARI_CONTROLLER", "OBSERVED"),
                make_valid_receipt(root["message_id"], root["content_sha256"], "LARI_CONTROLLER", "VERIFIED"),
                make_valid_receipt(root["message_id"], root["content_sha256"], "LARI_CONTROLLER", "ACKNOWLEDGED"),
                rcpt,
            ],
            outbound_replies=[reply],
        )
        assert rcpt_res.is_valid is True

    def test_relay_message_cannot_grant_authority_invariant(self):
        msg = make_valid_message()
        rcpt = make_valid_receipt()
        assert RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY(msg) is True
        assert RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY(rcpt) is True


# ==============================================================================
# FAIL-CLOSED TESTS
# ==============================================================================

class TestControllerRelayFailClosed:
    def test_content_hash_tamper(self):
        msg = make_valid_message()
        msg["subject"] = "Tampered subject text"
        res = validate_controller_relay_message(msg)
        assert res.is_valid is False
        assert any("content_sha256 mismatch" in e for e in res.errors)

    def test_duplicate_message_id(self):
        m1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        m2 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        res = validate_channel_sequence_history([m1, m2])
        assert res.is_valid is False
        assert any("Duplicate message_id" in e for e in res.errors)

    def test_duplicate_directed_channel_sequence(self):
        m1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        m2 = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=1,
            message_id="CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000002",
        )
        res = validate_channel_sequence_history([m1, m2])
        assert res.is_valid is False
        assert any("Duplicate sequence" in e for e in res.errors)

    def test_sequence_gap(self):
        m1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        m2 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=3)
        res = validate_channel_sequence_history([m1, m2])
        assert res.is_valid is False
        assert any("Sequence gap" in e for e in res.errors)

    def test_sequence_regression(self):
        m1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        m2 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=2)
        m3 = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=1,
            message_id="CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000099",
        )
        res = validate_channel_sequence_history([m1, m2, m3])
        assert res.is_valid is False
        assert any("Sequence regression" in e for e in res.errors)

    def test_malformed_deterministic_id(self):
        with pytest.raises(ValueError, match="Malformed message_id format"):
            parse_message_id("INVALID-MESSAGE-ID")

    def test_wrong_reply_sender_receiver(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        wrong_reply = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=2,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
        )
        res = validate_thread_history([root, wrong_reply])
        assert res.is_valid is False
        assert any("Invalid direction inversion" in e for e in res.errors)

    def test_unknown_in_reply_to(self):
        reply = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id="CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            in_reply_to="CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        )
        res = validate_thread_history([reply])
        assert res.is_valid is False
        assert any("unknown in_reply_to message" in e for e in res.errors)

    def test_cross_thread_reply(self):
        root1 = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        root2 = make_valid_message(from_c="LARI_CONTROLLER", to_c="AOS_CONTROLLER", sequence=1)
        cross_reply = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=2,
            thread_id=root2["message_id"],
            in_reply_to=root1["message_id"],
        )
        res = validate_thread_history([root1, root2, cross_reply])
        assert res.is_valid is False
        assert any("Cross-thread reply" in e for e in res.errors)

    def test_self_reply(self):
        msg_id = "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
        self_reply = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=1,
            message_id=msg_id,
            thread_id=msg_id,
            in_reply_to=msg_id,
        )
        res = validate_thread_history([self_reply])
        assert res.is_valid is False
        assert any("Self-referencing reply" in e for e in res.errors)

    def test_stale_superseded_reply(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        sup = make_valid_message(
            from_c="AOS_CONTROLLER",
            to_c="LARI_CONTROLLER",
            sequence=2,
            thread_id=root["message_id"],
            in_reply_to=None,
            supersedes_message_id=root["message_id"],
        )
        stale_reply = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
        )
        res = validate_thread_history([root, sup, stale_reply])
        assert res.is_valid is False
        assert any("stale superseded message" in e for e in res.errors)

    def test_invalid_supersession(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        invalid_sup = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=root["message_id"],
            in_reply_to=None,
            supersedes_message_id=root["message_id"],
        )
        res = validate_thread_history([root, invalid_sup])
        assert res.is_valid is False
        assert any("does not match target superseded controller" in e for e in res.errors)

    def test_duplicate_receipt(self):
        msg = make_valid_message()
        r1 = make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "OBSERVED")
        r2 = make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "OBSERVED")
        res = validate_receipt_lifecycle(msg, [r1, r2])
        assert res.is_valid is False
        assert any("Duplicate receipt event" in e for e in res.errors)

    def test_receipt_before_predecessor_lifecycle_event(self):
        msg = make_valid_message()
        r_ack = make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "ACKNOWLEDGED")
        res = validate_receipt_lifecycle(msg, [r_ack])
        assert res.is_valid is False
        assert any("expected predecessor event" in e for e in res.errors)

    def test_receipt_message_hash_mismatch(self):
        msg = make_valid_message()
        r1 = make_valid_receipt(msg["message_id"], "f" * 64, "LARI_CONTROLLER", "OBSERVED")
        res = validate_receipt_lifecycle(msg, [r1])
        assert res.is_valid is False
        assert any("message_content_sha256 mismatch" in e for e in res.errors)

    def test_unknown_protocol(self):
        msg = make_valid_message()
        msg["protocol"] = "UNKNOWN_PROTOCOL"
        res = validate_controller_relay_message(msg)
        assert res.is_valid is False
        assert any("protocol" in e.lower() for e in res.errors)

    def test_authority_effect_not_none(self):
        msg = make_valid_message(authority_effect="GRANT_FULL_AUTHORITY")
        res = validate_controller_relay_message(msg)
        assert res.is_valid is False
        assert any("authority_effect" in e.lower() for e in res.errors)

    def test_transport_replay_after_consumed(self):
        msg = make_valid_message()
        msg["is_replay_attempt"] = True
        rcpt = make_valid_receipt(msg["message_id"], msg["content_sha256"], "LARI_CONTROLLER", "CONSUMED")
        res = detect_relay_conflicts_and_replays([msg], [rcpt])
        assert res.is_valid is False
        assert any("Transport replay detected" in e for e in res.errors)

    def test_ambiguous_conflicting_decisions(self):
        root = make_valid_message(from_c="AOS_CONTROLLER", to_c="LARI_CONTROLLER", sequence=1)
        r1 = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=1,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
            decision="PROCEED",
        )
        r2 = make_valid_message(
            from_c="LARI_CONTROLLER",
            to_c="AOS_CONTROLLER",
            sequence=2,
            thread_id=root["message_id"],
            in_reply_to=root["message_id"],
            decision="REJECT",
        )
        res = detect_relay_conflicts_and_replays([root, r1, r2], [])
        assert res.is_valid is False
        assert res.disposition == "HOLD_AMBIGUOUS_CONCURRENT_DECISION"

    def test_utf8_bom_bearing_json(self):
        msg = make_valid_message()
        raw_json = json.dumps(msg).encode("utf-8")
        bom_raw = b"\xef\xbb\xbf" + raw_json
        res = validate_controller_relay_message_raw(bom_raw)
        assert res.is_valid is False
        assert any("BOM" in e for e in res.errors)

    def test_oversized_message(self):
        big_bytes = b"{" + (b" " * (65 * 1024)) + b"}"
        res = validate_controller_relay_message_raw(big_bytes)
        assert res.is_valid is False
        assert any("exceeds maximum limit" in e for e in res.errors)

    def test_prohibited_secret_like_structured_field(self):
        msg = make_valid_message()
        msg["subject"] = "My password bearer token is secret"
        msg["content_sha256"] = compute_message_content_sha256(msg)
        res = validate_controller_relay_message(msg)
        assert res.is_valid is False
        assert any("High-confidence credential pattern" in e or "Prohibited secret" in e for e in res.errors)

    def test_invalid_generic_controller_identity(self):
        with pytest.raises(ValueError, match="Invalid from_controller identity format"):
            format_message_id("invalid_lowercase_controller", "LARI_CONTROLLER", 1)


# ==============================================================================
# R1 RAW PARSER & STRICT DUPLICATE KEY TESTS
# ==============================================================================

class TestControllerRelayRawParsers:
    def test_raw_message_valid_pass(self):
        msg = make_valid_message()
        raw_bytes = json.dumps(msg).encode("utf-8")
        res = validate_controller_relay_message_raw(raw_bytes)
        assert res.is_valid is True
        assert res.disposition == "PASS"

    def test_raw_message_duplicate_top_level_key_fails(self):
        raw_json_str = """{
            "schema_version": "0.1.0",
            "protocol": "CONTROLLER_RELAY_V1",
            "protocol": "CONTROLLER_RELAY_V1",
            "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "sequence": 1,
            "from": "AOS_CONTROLLER",
            "to": "LARI_CONTROLLER",
            "in_reply_to": null,
            "created_at": "2026-09-03T08:00:00Z",
            "subject": "Duplicate key test",
            "subject_repository": "MertSGI/AOS",
            "subject_branch": "feature/controller-relay-v1",
            "subject_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
            "decision": "PROCEED",
            "authority_effect": "NONE",
            "authority_refs": [],
            "requested_next_action": "Evaluate",
            "requires_reply": true,
            "content_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
        }"""
        res = validate_controller_relay_message_raw(raw_json_str.encode("utf-8"))
        assert res.is_valid is False
        assert any("DUPLICATE_JSON_KEY" in e for e in res.errors)

    def test_raw_message_duplicate_nested_key_fails(self):
        raw_json_str = """{
            "schema_version": "0.1.0",
            "protocol": "CONTROLLER_RELAY_V1",
            "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "sequence": 1,
            "from": "AOS_CONTROLLER",
            "to": "LARI_CONTROLLER",
            "in_reply_to": null,
            "created_at": "2026-09-03T08:00:00Z",
            "subject": "Duplicate nested key test",
            "subject_repository": "MertSGI/AOS",
            "subject_branch": "feature/controller-relay-v1",
            "subject_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
            "decision": "PROCEED",
            "authority_effect": "NONE",
            "authority_refs": [],
            "requested_next_action": "Evaluate",
            "requires_reply": true,
            "content_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "meta": {
                "key": "value1",
                "key": "value2"
            }
        }"""
        res = validate_controller_relay_message_raw(raw_json_str.encode("utf-8"))
        assert res.is_valid is False
        assert any("DUPLICATE_JSON_KEY" in e for e in res.errors)

    def test_raw_receipt_valid_pass(self):
        rcpt = make_valid_receipt()
        raw_bytes = json.dumps(rcpt).encode("utf-8")
        res = validate_controller_relay_receipt_raw(raw_bytes)
        assert res.is_valid is True

    def test_raw_receipt_bom_fails(self):
        rcpt = make_valid_receipt()
        raw_bytes = b"\xef\xbb\xbf" + json.dumps(rcpt).encode("utf-8")
        res = validate_controller_relay_receipt_raw(raw_bytes)
        assert res.is_valid is False
        assert any("BOM" in e for e in res.errors)

    def test_raw_receipt_malformed_utf8_fails(self):
        res = validate_controller_relay_receipt_raw(b"\x80\x81\x82")
        assert res.is_valid is False
        assert any("Invalid UTF-8" in e for e in res.errors)

    def test_raw_receipt_oversized_fails(self):
        big_bytes = b"{" + (b" " * (65 * 1024)) + b"}"
        res = validate_controller_relay_receipt_raw(big_bytes)
        assert res.is_valid is False
        assert any("exceeds maximum limit" in e for e in res.errors)

    def test_raw_receipt_duplicate_top_level_key_fails(self):
        raw_str = """{
            "schema_version": "0.1.0",
            "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
            "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
            "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "message_content_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "message_commit_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
            "actor": "LARI_CONTROLLER",
            "event": "OBSERVED",
            "created_at": "2026-09-03T08:05:00Z"
        }"""
        res = validate_controller_relay_receipt_raw(raw_str.encode("utf-8"))
        assert res.is_valid is False
        assert any("DUPLICATE_JSON_KEY" in e for e in res.errors)

    def test_raw_receipt_duplicate_nested_key_fails(self):
        raw_str = """{
            "schema_version": "0.1.0",
            "protocol": "CONTROLLER_RELAY_RECEIPT_V1",
            "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
            "message_content_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "message_commit_sha": "7c4c75e32c0d7c43fc071b0eb872b2b73fdd3c1e",
            "actor": "LARI_CONTROLLER",
            "event": "OBSERVED",
            "created_at": "2026-09-03T08:05:00Z",
            "meta": {"key": "val1", "key": "val2"}
        }"""
        res = validate_controller_relay_receipt_raw(raw_str.encode("utf-8"))
        assert res.is_valid is False
        assert any("DUPLICATE_JSON_KEY" in e for e in res.errors)

    def test_raw_receipt_secret_key_fails(self):
        rcpt = make_valid_receipt()
        rcpt["api_key"] = "secret_value"
        res = validate_controller_relay_receipt_raw(json.dumps(rcpt).encode("utf-8"))
        assert res.is_valid is False
        assert any("Prohibited secret" in e for e in res.errors)

    def test_raw_receipt_credential_value_fails(self):
        rcpt = make_valid_receipt()
        rcpt["created_at"] = "2026-09-03T08:05:00Z bearer secret_token_value_xyz"
        res = validate_controller_relay_receipt_raw(json.dumps(rcpt).encode("utf-8"))
        assert res.is_valid is False
        assert any("High-confidence credential pattern" in e for e in res.errors)

    def test_raw_receipt_unknown_protocol_fails(self):
        rcpt = make_valid_receipt()
        rcpt["protocol"] = "UNKNOWN_RECEIPT_PROTOCOL"
        res = validate_controller_relay_receipt_raw(json.dumps(rcpt).encode("utf-8"))
        assert res.is_valid is False
        assert any("receipt protocol" in e.lower() for e in res.errors)


# ==============================================================================
# R1 PROTOCOL SOURCE-CONTRACT TESTS
# ==============================================================================

class TestProtocolSourceContract:
    @pytest.fixture(autouse=True)
    def load_protocol_text(self):
        assert PROTOCOL_MD_PATH.is_file(), f"PROTOCOL.md missing at {PROTOCOL_MD_PATH}"
        with open(PROTOCOL_MD_PATH, "r", encoding="utf-8") as f:
            self.protocol_text = f.read()

    def test_protocol_contains_mandatory_normative_strings(self):
        required_substrings = [
            "HOLD_RELAY_UNAVAILABLE",
            "MANUAL_BOOTSTRAP_OR_BREAK_GLASS",
            "MANDATORY_INBOUND_CHECK=YES",
            "CHECK_LATEST_UNCONSUMED_INBOUND_RELAY_BEFORE_CROSS_CONTROLLER_ACTION=YES",
            "RELAY_CONSUMED != AUTHORITY_CONSUMED",
            "FAST_FORWARD_COMPARE_AND_SWAP",
            "HOLD_AMBIGUOUS_CONCURRENT_DECISION",
            "authority_refs",
            "opaque",
        ]
        for sub in required_substrings:
            assert sub in self.protocol_text, f"PROTOCOL.md missing required normative string: '{sub}'"

    def test_protocol_negative_assertion_no_overbroad_break_glass_semantic(self):
        forbidden_pattern = "break-glass automatically emits direct Layer 3 canonical control_request"
        assert forbidden_pattern not in self.protocol_text, f"PROTOCOL.md contains forbidden over-broad semantic string: '{forbidden_pattern}'"

    def test_relay_message_cannot_grant_authority_invariant_holds(self):
        msg = make_valid_message()
        rcpt = make_valid_receipt()
        assert RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY(msg) is True
        assert RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY(rcpt) is True
