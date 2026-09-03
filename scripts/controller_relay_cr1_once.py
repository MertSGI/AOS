"""AOS Controller Relay CR-1 One-Shot Capability Handshake Runner (R0 Dry-Run + Live Invoker).

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-LIVE-INVOKER-R0-20260903-01

Provides:
- One-shot dry-run planning helper for AOS_ROOT and LARI_REPLY handshake roles
- Fixed immutable role-to-principal mapping (AOS_ROOT -> AOS_CONTROLLER, LARI_REPLY -> LARI_CONTROLLER)
- Pure dry-run calculation of canonical message payload, path derivation, and validation
- Absolute zero credential access, network transport, or live write mutations
- Live one-shot invoker (execute_cr1_live) that publishes through ControllerRelayService
- Read-only observers (observe_exact_cr1_root, observe_exact_cr1_reply)
- AOS direct reply verification (verify_cr1_reply)
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
from aos.controller_relay_service import ControllerPrincipal, ControllerRelayService, derive_message_path

CR1_SCHEMA_VERSION: str = "0.1"
CR1_SUBJECT_REPOSITORY: str = "MertSGI/AOS"
CR1_SUBJECT_BRANCH: str = "feature/controller-relay-v1"
CR1_SUBJECT_SHA: str = "039232ecf10948bf55a9d9dab665828b6c06f7c6"
CR1_EXPECTED_PARENT_SHA: str = "039232ecf10948bf55a9d9dab665828b6c06f7c6"
CR1_AUTHORITY_REFS: list[str] = [
    "LARI-AOS-CONTROLLER-RELAY-CR0-R1-20260903-01",
    "039232ecf10948bf55a9d9dab665828b6c06f7c6",
    "c3ee2f2c1510abdddd3de14bc879e5ba27dac835",
]

EXPECTED_LIVE_RELAY_HEAD: str = CR1_SUBJECT_SHA
ALLOWED_ROLES = {"AOS_ROOT", "LARI_REPLY"}

ROLE_PRINCIPAL_MAP = {
    "AOS_ROOT": ControllerPrincipal("AOS_CONTROLLER"),
    "LARI_REPLY": ControllerPrincipal("LARI_CONTROLLER"),
}

# Exact bootstrap SHA for AOS_ROOT expected_head enforcement
_BOOTSTRAP_SHA: str = "039232ecf10948bf55a9d9dab665828b6c06f7c6"

# Exact root and reply message IDs
_ROOT_MESSAGE_ID: str = "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
_REPLY_MESSAGE_ID: str = "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001"


def build_cr1_root_message_plan(
    created_at: Optional[datetime] = None,
) -> Tuple[Dict[str, Any], str, bytes]:
    """Derive canonical CR-1 root message dict, derived path, and raw bytes.

    Role: AOS_ROOT
    Principal: AOS_CONTROLLER
    """
    ts = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    msg: Dict[str, Any] = {
        "schema_version": CR1_SCHEMA_VERSION,
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "thread_id": "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001",
        "sequence": 1,
        "from": "AOS_CONTROLLER",
        "to": "LARI_CONTROLLER",
        "in_reply_to": None,
        "created_at": ts,
        "subject": "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE",
        "subject_repository": CR1_SUBJECT_REPOSITORY,
        "subject_branch": CR1_SUBJECT_BRANCH,
        "subject_sha": CR1_SUBJECT_SHA,
        "decision": "CR1_CAPABILITY_HANDSHAKE_REQUEST",
        "authority_effect": "NONE",
        "authority_refs": list(CR1_AUTHORITY_REFS),
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
        root_dict.get("schema_version") != CR1_SCHEMA_VERSION
        or root_dict.get("protocol") != "CONTROLLER_RELAY_V1"
        or root_dict.get("message_id") != "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
        or root_dict.get("thread_id") != "CRV1-AOS_CONTROLLER-LARI_CONTROLLER-000000000001"
        or root_dict.get("sequence") != 1
        or root_dict.get("from") != "AOS_CONTROLLER"
        or root_dict.get("to") != "LARI_CONTROLLER"
        or root_dict.get("in_reply_to") is not None
        or root_dict.get("subject") != "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"
        or root_dict.get("subject_repository") != CR1_SUBJECT_REPOSITORY
        or root_dict.get("subject_branch") != CR1_SUBJECT_BRANCH
        or root_dict.get("subject_sha") != CR1_SUBJECT_SHA
        or root_dict.get("decision") != "CR1_CAPABILITY_HANDSHAKE_REQUEST"
        or root_dict.get("authority_effect") != "NONE"
        or root_dict.get("authority_refs") != CR1_AUTHORITY_REFS
        or root_dict.get("requested_next_action") != "LARI_CONTROLLER_VERIFY_AND_REPLY"
        or root_dict.get("requires_reply") is not True
    ):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Observed root message identity/field mismatch")

    ts = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    reply_msg: Dict[str, Any] = {
        "schema_version": CR1_SCHEMA_VERSION,
        "protocol": "CONTROLLER_RELAY_V1",
        "message_id": "CRV1-LARI_CONTROLLER-AOS_CONTROLLER-000000000001",
        "thread_id": root_dict["thread_id"],
        "sequence": 1,
        "from": "LARI_CONTROLLER",
        "to": "AOS_CONTROLLER",
        "in_reply_to": root_dict["message_id"],
        "created_at": ts,
        "subject": "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE",
        "subject_repository": CR1_SUBJECT_REPOSITORY,
        "subject_branch": CR1_SUBJECT_BRANCH,
        "subject_sha": CR1_SUBJECT_SHA,
        "decision": "CR1_CAPABILITY_HANDSHAKE_ACCEPTED",
        "authority_effect": "NONE",
        "authority_refs": list(CR1_AUTHORITY_REFS),
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


# ---------------------------------------------------------------------------
# LIVE ONE-SHOT INVOKER (R0)
# ---------------------------------------------------------------------------


def execute_cr1_live(
    role: str,
    service: ControllerRelayService,
    expected_head: str,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Execute CR-1 handshake live one-shot publication through ControllerRelayService.

    Publication path: CR1 builder -> ControllerRelayService.publish_message -> GitDataCASRelayTransport.publish_record

    No credentials, no repository, no branch, no path, no token, no requester, no retry parameters.
    Fixed principal mapping: AOS_ROOT -> AOS_CONTROLLER, LARI_REPLY -> LARI_CONTROLLER.
    """
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Invalid role '{role}'. Allowed roles: {sorted(list(ALLOWED_ROLES))}")

    principal = ROLE_PRINCIPAL_MAP[role]

    # --- CAS head check (fail-closed) ---
    current_head = service.get_head()
    if current_head != expected_head:
        raise ValueError(
            f"HOLD_CAS_RACE: Current service head '{current_head}' != expected_head '{expected_head}'"
        )

    if role == "AOS_ROOT":
        # AOS_ROOT additionally requires expected_head == exact bootstrap SHA
        if expected_head != _BOOTSTRAP_SHA:
            raise ValueError(
                f"HOLD_CAS_RACE: AOS_ROOT expected_head '{expected_head}' != bootstrap SHA '{_BOOTSTRAP_SHA}'"
            )

        # Build root message
        msg_dict, path, raw_bytes = build_cr1_root_message_plan(created_at=created_at)

        # Validate generated raw bytes
        val_res = validate_controller_relay_message_raw(raw_bytes)
        if not val_res.is_valid:
            raise ValueError(f"Root message raw validation failed: {val_res.errors}")

        # Publish through service
        pub_result = service.publish_message(raw_bytes, expected_head, principal)

        if not pub_result.is_valid or pub_result.disposition != "PASS":
            # Propagate accepted disposition without retry
            if pub_result.disposition == "HOLD_CAS_RACE":
                raise ValueError(f"HOLD_CAS_RACE: {pub_result.errors}")
            raise ValueError(f"{pub_result.disposition}: {pub_result.errors}")

        publication_commit_sha = pub_result.details["commit_sha"]

        return {
            "mode": "LIVE",
            "role": role,
            "principal_controller_id": principal.controller_id,
            "message_id": msg_dict["message_id"],
            "path": path,
            "content_sha256": msg_dict["content_sha256"],
            "publication_commit_sha": publication_commit_sha,
            "parent_sha": expected_head,
            "authority_effect": msg_dict["authority_effect"],
            "validation_disposition": pub_result.disposition,
        }

    else:  # LARI_REPLY
        # Live LARI_REPLY must obtain root through observe_exact_cr1_root
        root_observation = observe_exact_cr1_root(service, expected_head)

        # The expected head for reply publication MUST be the root publication commit
        reply_expected_head = root_observation["publication_commit_sha"]
        if reply_expected_head != expected_head:
            raise ValueError(
                f"HOLD_CAS_RACE: Root publication commit '{reply_expected_head}' != expected_head '{expected_head}'"
            )

        # Build reply from observed immutable root raw bytes
        observed_root_raw = root_observation["raw_bytes"]
        reply_msg_dict, reply_path, reply_raw_bytes = build_cr1_reply_message_plan(
            observed_root_raw, created_at=created_at
        )

        # Validate reply raw bytes
        val_res = validate_controller_relay_message_raw(reply_raw_bytes)
        if not val_res.is_valid:
            raise ValueError(f"Reply message raw validation failed: {val_res.errors}")

        # Publish through service
        pub_result = service.publish_message(reply_raw_bytes, expected_head, principal)

        if not pub_result.is_valid or pub_result.disposition != "PASS":
            if pub_result.disposition == "HOLD_CAS_RACE":
                raise ValueError(f"HOLD_CAS_RACE: {pub_result.errors}")
            raise ValueError(f"{pub_result.disposition}: {pub_result.errors}")

        publication_commit_sha = pub_result.details["commit_sha"]

        return {
            "mode": "LIVE",
            "role": role,
            "principal_controller_id": principal.controller_id,
            "message_id": reply_msg_dict["message_id"],
            "path": reply_path,
            "content_sha256": reply_msg_dict["content_sha256"],
            "publication_commit_sha": publication_commit_sha,
            "parent_sha": expected_head,
            "authority_effect": reply_msg_dict["authority_effect"],
            "validation_disposition": pub_result.disposition,
        }


# ---------------------------------------------------------------------------
# EXACT OBSERVERS (READ-ONLY)
# ---------------------------------------------------------------------------


def observe_exact_cr1_root(
    service: ControllerRelayService,
    expected_head: str,
) -> Dict[str, Any]:
    """Read-only observation of exact CR1 root message through service.

    1. service.get_head() must equal expected_head.
    2. service.list_message_provenances(ref=expected_head) triggers complete history validation.
    3. Locate exactly one message with root message_id.
    4. Read actual raw immutable bytes through service.read_record.
    5. Validate raw bytes and enforce exact R0-R1 root binding.

    Returns internal observation with parsed message, raw bytes, path, publication_commit_sha.
    """
    # 1. Head check
    current_head = service.get_head()
    if current_head != expected_head:
        raise ValueError(
            f"HOLD_CAS_RACE: Current service head '{current_head}' != expected_head '{expected_head}'"
        )

    # 2. Complete service history validation
    provenances = service.list_message_provenances(ref=expected_head)

    # 3. Locate exactly one root message
    root_entries = [
        (msg, pub_sha)
        for msg, pub_sha in provenances
        if msg.get("message_id") == _ROOT_MESSAGE_ID
    ]

    if len(root_entries) == 0:
        raise ValueError(
            f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Root message '{_ROOT_MESSAGE_ID}' not found"
        )
    if len(root_entries) > 1:
        raise ValueError(
            f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Duplicate/ambiguous root message '{_ROOT_MESSAGE_ID}'"
        )

    root_msg, root_pub_sha = root_entries[0]

    # 4. Derive canonical path and read actual raw immutable bytes
    root_path = derive_message_path(root_msg)
    root_raw_bytes = service.read_record(root_path, ref=expected_head)

    # 5. Validate raw bytes
    val_res = validate_controller_relay_message_raw(root_raw_bytes)
    if not val_res.is_valid:
        raise ValueError(f"HOLD_INVALID_OBSERVED_ROOT: Root raw validation failed: {val_res.errors}")

    # 6. Enforce exact R0-R1 root binding
    parsed_root, _ = _parse_raw_relay_bytes_strict(root_raw_bytes)
    if not isinstance(parsed_root, dict):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Root payload is not a valid JSON dict")

    if (
        parsed_root.get("schema_version") != CR1_SCHEMA_VERSION
        or parsed_root.get("protocol") != "CONTROLLER_RELAY_V1"
        or parsed_root.get("message_id") != _ROOT_MESSAGE_ID
        or parsed_root.get("from") != "AOS_CONTROLLER"
        or parsed_root.get("to") != "LARI_CONTROLLER"
        or parsed_root.get("sequence") != 1
        or parsed_root.get("thread_id") != _ROOT_MESSAGE_ID
        or parsed_root.get("in_reply_to") is not None
        or parsed_root.get("subject") != "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"
        or parsed_root.get("subject_repository") != CR1_SUBJECT_REPOSITORY
        or parsed_root.get("subject_branch") != CR1_SUBJECT_BRANCH
        or parsed_root.get("subject_sha") != CR1_SUBJECT_SHA
        or parsed_root.get("decision") != "CR1_CAPABILITY_HANDSHAKE_REQUEST"
        or parsed_root.get("authority_effect") != "NONE"
        or parsed_root.get("authority_refs") != CR1_AUTHORITY_REFS
        or parsed_root.get("requested_next_action") != "LARI_CONTROLLER_VERIFY_AND_REPLY"
        or parsed_root.get("requires_reply") is not True
    ):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Exact R0-R1 root binding mismatch")

    # 7. Require parsed content and provenance refer to same message
    if parsed_root.get("message_id") != root_msg.get("message_id"):
        raise ValueError("HOLD_INVALID_OBSERVED_ROOT: Parsed root and provenance message_id mismatch")

    return {
        "parsed_message": parsed_root,
        "raw_bytes": root_raw_bytes,
        "path": root_path,
        "publication_commit_sha": root_pub_sha,
    }


def observe_exact_cr1_reply(
    service: ControllerRelayService,
    expected_head: str,
) -> Dict[str, Any]:
    """Read-only observation of exact CR1 reply message through service.

    Same principles as root observer: service head exact, complete history validation,
    exact immutable record lookup, raw read through service, canonical raw validation.
    """
    # 1. Head check
    current_head = service.get_head()
    if current_head != expected_head:
        raise ValueError(
            f"HOLD_CAS_RACE: Current service head '{current_head}' != expected_head '{expected_head}'"
        )

    # 2. Complete service history validation
    provenances = service.list_message_provenances(ref=expected_head)

    # 3. Locate exactly one reply message
    reply_entries = [
        (msg, pub_sha)
        for msg, pub_sha in provenances
        if msg.get("message_id") == _REPLY_MESSAGE_ID
    ]

    if len(reply_entries) == 0:
        raise ValueError(
            f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Reply message '{_REPLY_MESSAGE_ID}' not found"
        )
    if len(reply_entries) > 1:
        raise ValueError(
            f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Duplicate/ambiguous reply message '{_REPLY_MESSAGE_ID}'"
        )

    reply_msg, reply_pub_sha = reply_entries[0]

    # 4. Derive canonical path and read actual raw immutable bytes
    reply_path = derive_message_path(reply_msg)
    reply_raw_bytes = service.read_record(reply_path, ref=expected_head)

    # 5. Validate raw bytes
    val_res = validate_controller_relay_message_raw(reply_raw_bytes)
    if not val_res.is_valid:
        raise ValueError(f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Reply raw validation failed: {val_res.errors}")

    # 6. Parse and validate
    parsed_reply, _ = _parse_raw_relay_bytes_strict(reply_raw_bytes)
    if not isinstance(parsed_reply, dict):
        raise ValueError("HOLD_CROSS_CONTROLLER_HANDSHAKE: Reply payload is not a valid JSON dict")

    # 7. Require parsed content and provenance refer to same message
    if parsed_reply.get("message_id") != reply_msg.get("message_id"):
        raise ValueError("HOLD_CROSS_CONTROLLER_HANDSHAKE: Parsed reply and provenance message_id mismatch")

    return {
        "parsed_message": parsed_reply,
        "raw_bytes": reply_raw_bytes,
        "path": reply_path,
        "publication_commit_sha": reply_pub_sha,
    }


def verify_cr1_reply(
    service: ControllerRelayService,
    expected_head: str,
) -> Dict[str, Any]:
    """AOS direct reply verification — read-only.

    Uses observe_exact_cr1_reply and enforces exact reply contract fields.
    """
    observation = observe_exact_cr1_reply(service, expected_head)
    parsed_reply = observation["parsed_message"]

    # Enforce EXACT reply contract
    checks = [
        ("schema_version", CR1_SCHEMA_VERSION),
        ("message_id", _REPLY_MESSAGE_ID),
        ("from", "LARI_CONTROLLER"),
        ("to", "AOS_CONTROLLER"),
        ("sequence", 1),
        ("thread_id", _ROOT_MESSAGE_ID),
        ("in_reply_to", _ROOT_MESSAGE_ID),
        ("subject", "CONTROLLER_RELAY_CR1_CAPABILITY_HANDSHAKE"),
        ("subject_repository", CR1_SUBJECT_REPOSITORY),
        ("subject_branch", CR1_SUBJECT_BRANCH),
        ("subject_sha", CR1_SUBJECT_SHA),
        ("decision", "CR1_CAPABILITY_HANDSHAKE_ACCEPTED"),
        ("authority_effect", "NONE"),
        ("authority_refs", list(CR1_AUTHORITY_REFS)),
        ("requested_next_action", "AOS_CONTROLLER_VERIFY_REPLY_AND_CLOSE_HANDSHAKE"),
        ("requires_reply", False),
    ]

    for field, expected_val in checks:
        actual_val = parsed_reply.get(field)
        if actual_val != expected_val:
            raise ValueError(
                f"HOLD_CROSS_CONTROLLER_HANDSHAKE: Reply field '{field}' mismatch: "
                f"expected {expected_val!r}, got {actual_val!r}"
            )

    return {
        "verification_disposition": "PASS",
        "message_id": parsed_reply["message_id"],
        "content_sha256": parsed_reply["content_sha256"],
        "publication_commit_sha": observation["publication_commit_sha"],
        "path": observation["path"],
        "authority_effect": "NONE",
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
