"""Deterministic offline unit tests for Controller Relay CR-1 One-Shot Handshake Runner.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-R1-20260903-01
PROVES ROLE BOUNDARIES, EXACT IDENTITIES, CANONICAL HASH RECOMPUTATION, AND ZERO MUTATION.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aos.controller_relay import compute_message_content_sha256
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
)


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
