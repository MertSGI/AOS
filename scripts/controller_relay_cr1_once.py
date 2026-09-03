"""AOS Controller Relay CR-1 One-Shot Capability Handshake Runner (R0 Dry-Run Only).

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-20260903-01

Provides:
- One-shot dry-run planning helper for AOS_ROOT and LARI_REPLY handshake roles
- Fixed immutable role-to-principal mapping (AOS_ROOT -> AOS_CONTROLLER, LARI_REPLY -> LARI_CONTROLLER)
- Pure dry-run calculation of canonical message payload, path derivation, and validation
- Absolute zero credential access, network transport, or live write mutations
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from aos.controller_relay import (
    _parse_raw_relay_bytes_strict,
    compute_message_content_sha256,
    validate_controller_relay_message_raw,
)
from aos.controller_relay_service import ControllerPrincipal, derive_message_path

EXPECTED_LIVE_RELAY_HEAD: str = "039232ecf10948bf55a9d9dab665828b6c06f7c6"
ALLOWED_ROLES = {"AOS_ROOT", "LARI_REPLY"}

ROLE_PRINCIPAL_MAP = {
    "AOS_ROOT": ControllerPrincipal("AOS_CONTROLLER"),
    "LARI_REPLY": ControllerPrincipal("LARI_CONTROLLER"),
}


def build_cr1_root_message_plan(
    created_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], str, bytes]:
    """Derive canonical CR-1 root message dict, derived path, and raw bytes.

    Role: AOS_ROOT
    Principal: AOS_CONTROLLER
    """
    ts = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    msg: Dict[str, Any] = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "sequence": 1,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "created_at": ts,
        "subject": "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "feature/controller-relay-service-v1",
        "subject_sha": EXPECTED_LIVE_RELAY_HEAD,
        "decision": "CR1_CAPABILITY_HANDSHAKE_REQUEST",
        "authority_effect": "NONE",
        "authority_refs": [
            "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01",
            EXPECTED_LIVE_RELAY_HEAD,
            "c3ee2f2c1510abdddd3de14bc879e5ba27dac835",
        ],
        "requested_next_action": "LARI_CONTROLLER_VERIFY_AND_REPLY",
        "requires_reply": True,
    }

    msg["content_sha256"] = compute_message_content_sha256(msg)
    raw_bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    path = derive_message_path(msg)

    return msg, path, raw_bytes


def build_cr1_reply_message_plan(
    observed_root_raw: bytes,
    created_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], str, bytes]:
    """Derive canonical CR-1 reply message dict, derived path, and raw bytes from validated observed root.

    Role: LARI_REPLY
    Principal: LARI_CONTROLLER
    """
    # 1. Validate raw observed root
    val_root = validate_controller_relay_message_raw(observed_root_raw)
    if not val_root.is_valid:
        raise ValueError(f"HOLD_INVALID_OBSERVED_ROOT: Raw validation failed: {val_root.errors}")

    root_dict, _ = _parse_raw_relay_bytes_strict(observed_root_raw)
    if not isinstance(root_dict, dict):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Root payload is not a valid JSON dict")

    # 2. Require exact root identity checks
    if (
        root_dict.get("message_id") != "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
        or root_dict.get("from") != "AOS_CONTROLLER"
        or root_dict.get("to") != "LARI_CONTROLLER"
        or root_dict.get("sequence") != 1
        or root_dict.get("thread_id") != "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
        or root_dict.get("in_reply_to") is not None
        or root_dict.get("subject") != "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"
        or root_dict.get("decision") != "CR1_CAPABILITY_HANDSHAKE_REQUEST"
        or root_dict.get("authority_effect") != "NONE"
        or root_dict.get("requires_reply") is not True
    ):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Observed root message identity/field mismatch")

    ts = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    reply_msg: Dict[str, Any] = {
        "schema_version": "0.1.0",
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001",
        "thread_id": root_dict["thread_id"],
        "sequence": 1,
        "from": "LARI_CONTROLLER",
        "to": "AOS_CONTROLLER",
        "in_reply_to": root_dict["message_id"],
        "created_at": ts,
        "subject": "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE",
        "subject_repository": "MertSGI/AOS",
        "subject_branch": "feature/controller-relay-service-v1",
        "subject_sha": EXPECTED_LIVE_RELAY_HEAD,
        "decision": "CR1_CAPABILITY_HANDSHAKE_ACCEPTED",
        "authority_effect": "NONE",
        "authority_refs": [
            "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01",
            EXPECTED_LIVE_RELAY_HEAD,
            "c3ee2f2c1510abdddd3de14bc879e5ba27dac835",
        ],
        "requested_next_action": "AOS_CONTROLLER_VERIFY_REPLY_AND_CLOSE_HANDSHAKE",
        "requires_reply": False,
    }

    reply_msg["content_sha256"] = compute_message_content_sha256(reply_msg)
    raw_bytes = json.dumps(reply_msg, ensure_ascii=False).encode("utf-8")
    path = derive_message_path(reply_msg)

    return reply_msg, path, raw_bytes


def execute_cr1_dry_run(
    role: str,
    observed_root_raw: Optional[bytes] = None,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Execute CR-1 handshake plan creation in strict DRY_RUN mode.

    Zero credentials, zero network, zero live transport mutations.
    """
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Invalid role '{role}'. Allowed roles: {sorted(list(ALLOWED_ROLES))}")

    principal = ROLE_PRINCIPAL_MAP[role]

    if role == "AOS_ROOT":
        msg_dict, path, raw_bytes = build_cr1_root_message_plan(created_at=created_at)
    else:  # LARI_REPLY
        if not observed_root_raw:
            raise ValueError("HOLD_INVALID_OBSERVED_ROOT: LARI_REPLY requires valid observed_root_raw input")
        msg_dict, path, raw_bytes = build_cr1_reply_message_plan(observed_root_raw, created_at=created_at)

    val_res = validate_controller_relay_message_raw(raw_bytes)
    if not val_res.is_valid:
        raise ValueError(f"CR-1 message raw validation failed: {val_res.errors}")

    return {
        "mode": "DRY_RUN",
        "role": role,
        "principal_controller_id": principal.controller_id,
        "message_id": msg_dict["message_id"],
        "derived_message_path": path,
        "sequence": msg_dict["sequence"],
        "thread_id": msg_dict["thread_id"],
        "content_sha256": msg_dict["content_sha256"],
        "expected_parent_sha": EXPECTED_LIVE_RELAY_HEAD,
        "authority_effect": msg_dict["authority_effect"],
        "validation_disposition": val_res.disposition,
        "CREDENTIAL_ACCESS_COUNT": 0,
        "TRANSPORT_MUTATION_COUNT": 0,
        "LIVE_RELAY_WRITE_COUNT": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="AOS Controller Relay CR-1 One-Shot Handshake Runner (R0 Dry-Run)")
    parser.add_argument("--role", required=True, choices=["AOS_ROOT", "LARI_REPLY"], help="Handshake role")
    parser.add_argument("--observed-root-file", help="Path to observed root JSON message file for LARI_REPLY role")

    args = parser.parse_args()

    observed_root_raw = None
    if args.role == "LARI_REPLY":
        if not args.observed_root_file:
            sys.stderr.write("Error: --observed-root-file is required for LARI_REPLY role\n")
            sys.exit(1)
        with open(args.observed_root_file, "rb") as f:
            observed_root_raw = f.read()

    try:
        result = execute_cr1_dry_run(args.role, observed_root_raw=observed_root_raw)
        print(json.dumps(result, indent=2))
    except Exception as exc:
        sys.stderr.write(f"Dry-run execution failed: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
