"""AOS Controller Relay V1 deterministic transport validation engine and receipt state machine.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-CR0-20260903-01
CR-0 establishes protocol specifications, schemas, deterministic network-free validation,
derived receipt state-machine logic, and CLI registration.

NON-AUTHORITY INVARIANT:
RELAY_MESSAGE != AUTHORITY
CONTROLLER_INBOX_CLAIM != AUTHORITY
CANDIDATE_SHA != AUTHORITY
RELAY_RECEIPT != AUTHORITY
RELAY_CONSUMED != AUTHORITY_CONSUMED
Relay V1 authority_effect MUST be strictly "NONE".
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from aos.validate import validate_document

MAX_RELAY_MESSAGE_BYTES: int = 64 * 1024  # 64 KiB conservative payload limit

PROHIBITED_SECRET_KEY_PATTERNS: Set[str] = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "client_secret",
    "authorization",
    "bearer_token",
}

CREDENTIAL_VALUE_PATTERNS: List[re.Pattern] = [
    re.compile(r"bearer\s+[a-zA-Z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"sk_[live|test]_[a-zA-Z0-9]{24,}"),
]

CONTROLLER_ID_REGEX = re.compile(r"^[A-Z0-9_]+_CONTROLLER$")
MESSAGE_ID_REGEX = re.compile(r"^CRV1-([A-Z0-9_]+_CONTROLLER)-([A-Z0-9_]+_CONTROLLER)-([0-9]{12})$")
GIT_SHA_REGEX = re.compile(r"^[0-9a-f]{40}$")
SHA256_HEX_REGEX = re.compile(r"^[0-9a-f]{64}$")

RECEIPT_EVENT_ORDER: List[str] = ["OBSERVED", "VERIFIED", "ACKNOWLEDGED", "CONSUMED"]


class ControllerRelayError(Exception):
    """Base exception for controller relay validation failures."""
    pass


class ControllerRelayValidationResult:
    """Result of deterministic Controller Relay validation checks."""

    def __init__(
        self,
        is_valid: bool,
        disposition: str,
        errors: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.is_valid = is_valid
        self.disposition = disposition  # e.g., "PASS", "FAIL", "HOLD_AMBIGUOUS_CONCURRENT_DECISION"
        self.errors = errors or []
        self.warnings = warnings or []
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "disposition": self.disposition,
            "errors": self.errors,
            "warnings": self.warnings,
            "details": self.details,
        }


def format_message_id(from_controller: str, to_controller: str, sequence: int) -> str:
    """Format a deterministic Controller Relay V1 message ID."""
    if not CONTROLLER_ID_REGEX.match(from_controller):
        raise ValueError(f"Invalid from_controller identity format: '{from_controller}'")
    if not CONTROLLER_ID_REGEX.match(to_controller):
        raise ValueError(f"Invalid to_controller identity format: '{to_controller}'")
    if not isinstance(sequence, int) or sequence < 1 or sequence > 999999999999:
        raise ValueError(f"Sequence must be integer between 1 and 999999999999, got: {sequence}")
    return f"CRV1-{from_controller}-{to_controller}-{sequence:012d}"


def parse_message_id(message_id: str) -> Tuple[str, str, int]:
    """Parse and validate components of a Controller Relay V1 message ID."""
    match = MESSAGE_ID_REGEX.match(message_id)
    if not match:
        raise ValueError(f"Malformed message_id format: '{message_id}'")
    from_c, to_c, seq_str = match.groups()
    return from_c, to_c, int(seq_str)


def canonicalize_message_content(message: Dict[str, Any]) -> bytes:
    """Canonicalize message content for UTF-8 SHA-256 hashing.

    Excludes 'content_sha256' field. Uses sorted keys, compact separators, no BOM, no trailing newline.
    """
    cleaned = {k: v for k, v in message.items() if k != "content_sha256"}
    serialized = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return serialized.encode("utf-8")


def compute_message_content_sha256(message: Dict[str, Any]) -> str:
    """Compute exact lowercase 64-character SHA-256 hex digest of canonicalized message content."""
    content_bytes = canonicalize_message_content(message)
    return hashlib.sha256(content_bytes).hexdigest()


def verify_message_content_sha256(message: Dict[str, Any]) -> bool:
    """Verify that message.content_sha256 matches recomputed canonical SHA-256."""
    expected = message.get("content_sha256")
    if not isinstance(expected, str) or not SHA256_HEX_REGEX.match(expected):
        return False
    computed = compute_message_content_sha256(message)
    return computed == expected


def scan_for_prohibited_secrets(raw_input: Any, current_path: str = "") -> List[str]:
    """Recursively scan structured JSON data or string for secret/credential material.

    Returns list of detected violation paths/reasons WITHOUT revealing secret values.
    """
    violations: List[str] = []

    if isinstance(raw_input, dict):
        for key, val in raw_input.items():
            path = f"{current_path}.{key}" if current_path else key
            normalized_key = key.lower().replace("-", "_")
            if normalized_key in PROHIBITED_SECRET_KEY_PATTERNS:
                violations.append(f"Prohibited secret key detected at path '{path}'")
            violations.extend(scan_for_prohibited_secrets(val, path))
    elif isinstance(raw_input, list):
        for idx, item in enumerate(raw_input):
            path = f"{current_path}[{idx}]"
            violations.extend(scan_for_prohibited_secrets(item, path))
    elif isinstance(raw_input, str):
        for pattern in CREDENTIAL_VALUE_PATTERNS:
            if pattern.search(raw_input):
                violations.append(f"High-confidence credential pattern matched in text at path '{current_path}'")
                break

    return violations


def validate_controller_relay_message_raw(raw_bytes: bytes) -> ControllerRelayValidationResult:
    """Validate raw serialized UTF-8 bytes of a Controller Relay V1 message."""
    errors: List[str] = []

    if len(raw_bytes) > MAX_RELAY_MESSAGE_BYTES:
        errors.append(f"Message size ({len(raw_bytes)} bytes) exceeds maximum limit ({MAX_RELAY_MESSAGE_BYTES} bytes)")
        return ControllerRelayValidationResult(False, "FAIL", errors)

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM header is strictly prohibited")
        return ControllerRelayValidationResult(False, "FAIL", errors)

    try:
        raw_text = raw_bytes.decode("utf-8")
        data = json.loads(raw_text)
    except Exception as exc:
        errors.append(f"Invalid UTF-8 JSON encoding: {exc}")
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return validate_controller_relay_message(data)


def validate_controller_relay_message(message: Dict[str, Any]) -> ControllerRelayValidationResult:
    """Validate a parsed Controller Relay message dict against schema, invariants, and rules."""
    errors: List[str] = []

    # 1. Prohibited secret scanning (checked first so secret keys in payloads trigger secret defense)
    secret_violations = scan_for_prohibited_secrets(message)
    if secret_violations:
        errors.extend(secret_violations)

    # 2. JSON Schema validation
    schema_res = validate_document("controller_relay_message", message)
    if not schema_res.is_valid:
        for err in schema_res.errors:
            errors.append(f"Schema validation error: {err}")

    # 3. Protocol exact match
    if message.get("protocol") != "CONTROLLER_RELAY_V1":
        errors.append(f"Invalid protocol: '{message.get('protocol')}', expected 'CONTROLLER_RELAY_V1'")

    # 4. Authority effect exact NONE
    if message.get("authority_effect") != "NONE":
        errors.append(f"Invalid authority_effect: '{message.get('authority_effect')}', MUST be 'NONE'")

    # 5. Message ID syntax and component matching
    msg_id = message.get("message_id", "")
    from_c = message.get("from", "")
    to_c = message.get("to", "")
    seq = message.get("sequence", 0)

    try:
        parsed_from, parsed_to, parsed_seq = parse_message_id(msg_id)
        if parsed_from != from_c:
            errors.append(f"message_id FROM component ('{parsed_from}') does not match message from field ('{from_c}')")
        if parsed_to != to_c:
            errors.append(f"message_id TO component ('{parsed_to}') does not match message to field ('{to_c}')")
        if parsed_seq != seq:
            errors.append(f"message_id sequence component ({parsed_seq}) does not match message sequence ({seq})")
    except ValueError as val_err:
        errors.append(str(val_err))

    # 6. Canonical hash verification
    if not verify_message_content_sha256(message):
        errors.append("message content_sha256 mismatch with recomputed canonical content hash")

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def validate_controller_relay_receipt(receipt: Dict[str, Any]) -> ControllerRelayValidationResult:
    """Validate a parsed Controller Relay receipt dict against schema and secrets."""
    errors: List[str] = []

    # 1. Secret scanning
    secret_violations = scan_for_prohibited_secrets(receipt)
    if secret_violations:
        errors.extend(secret_violations)

    # 2. JSON Schema validation
    schema_res = validate_document("controller_relay_receipt", receipt)
    if not schema_res.is_valid:
        for err in schema_res.errors:
            errors.append(f"Receipt schema validation error: {err}")

    # 3. Protocol exact match
    if receipt.get("protocol") != "CONTROLLER_RELAY_RECEIPT_V1":
        errors.append(f"Invalid receipt protocol: '{receipt.get('protocol')}', expected 'CONTROLLER_RELAY_RECEIPT_V1'")

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def validate_channel_sequence_history(messages: List[Dict[str, Any]]) -> ControllerRelayValidationResult:
    """Validate directed channel sequence integrity across a history of messages."""
    errors: List[str] = []
    # Map (from, to) -> last_sequence
    channel_last_seq: Dict[Tuple[str, str], int] = {}
    seen_message_ids: Set[str] = set()

    for idx, msg in enumerate(messages):
        msg_id = msg.get("message_id", f"INDEX_{idx}")
        if msg_id in seen_message_ids:
            errors.append(f"Duplicate message_id: '{msg_id}'")
            continue
        seen_message_ids.add(msg_id)

        from_c = msg.get("from")
        to_c = msg.get("to")
        seq = msg.get("sequence")

        if not from_c or not to_c or not isinstance(seq, int):
            errors.append(f"Message '{msg_id}' missing valid from/to/sequence fields")
            continue

        channel = (from_c, to_c)
        last_seq = channel_last_seq.get(channel, 0)

        if seq == last_seq:
            errors.append(f"Duplicate sequence {seq} on directed channel {from_c} -> {to_c} (message '{msg_id}')")
        elif seq < last_seq:
            errors.append(f"Sequence regression on directed channel {from_c} -> {to_c}: got {seq}, expected {last_seq + 1} (message '{msg_id}')")
        elif seq > last_seq + 1:
            errors.append(f"Sequence gap on directed channel {from_c} -> {to_c}: got {seq}, expected {last_seq + 1} (message '{msg_id}')")
        else:
            channel_last_seq[channel] = seq

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def validate_thread_history(messages: List[Dict[str, Any]]) -> ControllerRelayValidationResult:
    """Validate thread relationships, replies, direction inversion, and supersession rules."""
    errors: List[str] = []
    messages_by_id: Dict[str, Dict[str, Any]] = {}
    superseded_message_ids: Set[str] = set()
    thread_roots: Dict[str, str] = {}  # msg_id -> thread_id

    for msg in messages:
        # Validate individual message first
        msg_res = validate_controller_relay_message(msg)
        if not msg_res.is_valid:
            errors.extend(msg_res.errors)
            continue

        msg_id = msg["message_id"]
        thread_id = msg["thread_id"]
        in_reply_to = msg.get("in_reply_to")
        supersedes_id = msg.get("supersedes_message_id")
        from_c = msg["from"]
        to_c = msg["to"]

        messages_by_id[msg_id] = msg
        thread_roots[msg_id] = thread_id

        # 1. Root / Non-reply message validation
        if in_reply_to is None:
            if supersedes_id is None and thread_id != msg_id:
                errors.append(f"Root message '{msg_id}' thread_id ('{thread_id}') must equal message_id")
        else:
            # 2. Reply validation
            if in_reply_to == msg_id:
                errors.append(f"Self-referencing reply in message '{msg_id}'")
            elif in_reply_to not in messages_by_id:
                errors.append(f"Message '{msg_id}' references unknown in_reply_to message '{in_reply_to}'")
            else:
                prior_msg = messages_by_id[in_reply_to]
                # Cross-thread check
                if prior_msg["thread_id"] != thread_id:
                    errors.append(f"Cross-thread reply in message '{msg_id}': prior thread '{prior_msg['thread_id']}' vs reply thread '{thread_id}'")
                # Direction inversion check: reply.from == prior.to AND reply.to == prior.from
                if from_c != prior_msg["to"] or to_c != prior_msg["from"]:
                    errors.append(f"Invalid direction inversion in reply '{msg_id}': expected {prior_msg['to']} -> {prior_msg['from']}, got {from_c} -> {to_c}")
                # Stale superseded check
                if in_reply_to in superseded_message_ids:
                    errors.append(f"Message '{msg_id}' attempts to reply to stale superseded message '{in_reply_to}'")

        # 3. Supersession validation
        if supersedes_id:
            if supersedes_id == msg_id:
                errors.append(f"Message '{msg_id}' cannot supersede itself")
            elif supersedes_id not in messages_by_id:
                errors.append(f"Message '{msg_id}' references unknown supersedes_message_id '{supersedes_id}'")
            else:
                superseded_msg = messages_by_id[supersedes_id]
                if superseded_msg["from"] != from_c:
                    errors.append(f"Superseding message '{msg_id}' from controller '{from_c}' does not match target superseded controller '{superseded_msg['from']}'")
                if superseded_msg["thread_id"] != thread_id:
                    errors.append(f"Supersedes target '{supersedes_id}' is in different thread '{superseded_msg['thread_id']}' than superseding message '{thread_id}'")
                if supersedes_id in superseded_message_ids:
                    errors.append(f"Target message '{supersedes_id}' was already superseded")
                else:
                    superseded_message_ids.add(supersedes_id)

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def validate_receipt_lifecycle(
    message: Dict[str, Any],
    receipts: List[Dict[str, Any]],
    outbound_replies: Optional[List[Dict[str, Any]]] = None,
) -> ControllerRelayValidationResult:
    """Derive state and validate receipt lifecycle progression over immutable receipt records."""
    errors: List[str] = []
    msg_id = message.get("message_id", "")
    msg_hash = message.get("content_sha256", "")

    actor_event_seen: Set[Tuple[str, str, str]] = set()  # (actor, message_id, event)
    actor_last_event_idx: Dict[str, int] = {}  # actor -> max RECEIPT_EVENT_ORDER index

    for rcpt in receipts:
        # Validate individual receipt schema/rules
        rcpt_res = validate_controller_relay_receipt(rcpt)
        if not rcpt_res.is_valid:
            errors.extend(rcpt_res.errors)
            continue

        r_msg_id = rcpt.get("message_id")
        r_hash = rcpt.get("message_content_sha256")
        r_actor = rcpt.get("actor")
        r_event = rcpt.get("event")

        # 1. Target message check
        if r_msg_id != msg_id:
            errors.append(f"Receipt targeting unknown/mismatched message_id '{r_msg_id}', expected '{msg_id}'")
            continue

        # 2. Content hash mismatch check
        if r_hash != msg_hash:
            errors.append(f"Receipt message_content_sha256 mismatch for message '{msg_id}'")

        # 3. Duplicate event check
        key = (r_actor, r_msg_id, r_event)
        if key in actor_event_seen:
            errors.append(f"Duplicate receipt event '{r_event}' for actor '{r_actor}' on message '{msg_id}'")
            continue
        actor_event_seen.add(key)

        # 4. Strict linear progression check
        if r_event not in RECEIPT_EVENT_ORDER:
            errors.append(f"Unsupported receipt event: '{r_event}'")
            continue

        current_event_idx = RECEIPT_EVENT_ORDER.index(r_event)
        last_event_idx = actor_last_event_idx.get(r_actor, -1)

        if current_event_idx != last_event_idx + 1:
            expected_event = RECEIPT_EVENT_ORDER[last_event_idx + 1] if last_event_idx + 1 < len(RECEIPT_EVENT_ORDER) else "NONE"
            errors.append(
                f"Invalid receipt event progression for actor '{r_actor}': got '{r_event}', expected predecessor event '{expected_event}'"
            )
        else:
            actor_last_event_idx[r_actor] = current_event_idx

    # 5. Check CONSUMED requires reply condition if requires_reply=true
    if message.get("requires_reply") is True:
        for actor, max_idx in actor_last_event_idx.items():
            if max_idx == RECEIPT_EVENT_ORDER.index("CONSUMED"):
                # Must have an outbound reply or explicit decision response
                has_reply = False
                if outbound_replies:
                    for reply in outbound_replies:
                        if reply.get("in_reply_to") == msg_id or reply.get("thread_id") == message.get("thread_id"):
                            has_reply = True
                            break
                if not has_reply:
                    errors.append(
                        f"Receipt CONSUMED by actor '{actor}' on message '{msg_id}' requiring reply, but no valid outbound reply decision exists"
                    )

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def detect_relay_conflicts_and_replays(
    messages: List[Dict[str, Any]],
    receipts: List[Dict[str, Any]],
) -> ControllerRelayValidationResult:
    """Detect transport-consumed replays, duplicate sequences, and ambiguous concurrent decisions."""
    errors: List[str] = []

    # 1. Channel sequence check
    seq_res = validate_channel_sequence_history(messages)
    if not seq_res.is_valid:
        errors.extend(seq_res.errors)

    # 2. Thread history check
    thread_res = validate_thread_history(messages)
    if not thread_res.is_valid:
        errors.extend(thread_res.errors)

    # 3. Detect transport-consumed replay
    consumed_msg_ids: Set[str] = set()
    for rcpt in receipts:
        if rcpt.get("event") == "CONSUMED":
            consumed_msg_ids.add(rcpt.get("message_id", ""))

    for msg in messages:
        m_id = msg.get("message_id", "")
        # If a message is already CONSUMED in transport and attempted to be re-processed as new input
        if m_id in consumed_msg_ids and msg.get("is_replay_attempt"):
            errors.append(f"Transport replay detected for already consumed message '{m_id}'")

    # 4. Detect ambiguous concurrent decisions
    # Group live (non-superseded) reply decisions by target thread
    superseded_ids: Set[str] = {msg.get("supersedes_message_id") for msg in messages if msg.get("supersedes_message_id")}
    live_decisions_by_thread: Dict[str, List[Dict[str, Any]]] = {}

    for msg in messages:
        m_id = msg.get("message_id", "")
        if m_id in superseded_ids:
            continue
        if msg.get("in_reply_to"):
            thread_id = msg.get("thread_id", "")
            live_decisions_by_thread.setdefault(thread_id, []).append(msg)

    for thread_id, decisions in live_decisions_by_thread.items():
        if len(decisions) > 1:
            # Check if decisions have conflicting decision strings from the same reply stage
            decision_values = {d.get("decision") for d in decisions}
            if len(decision_values) > 1:
                return ControllerRelayValidationResult(
                    False,
                    "HOLD_AMBIGUOUS_CONCURRENT_DECISION",
                    [f"Multiple ambiguous concurrent live decisions found for thread '{thread_id}': {sorted(list(decision_values))}"],
                )

    if errors:
        return ControllerRelayValidationResult(False, "FAIL", errors)

    return ControllerRelayValidationResult(True, "PASS")


def RELAY_MESSAGE_CANNOT_GRANT_AUTHORITY(message_or_receipt: Dict[str, Any]) -> bool:
    """Explicitly verify that a relay record cannot produce or infer execution authority.

    Normative invariant: Relay V1 authority_effect MUST equal "NONE".
    Returns True if invariant strictly holds.
    """
    if "protocol" in message_or_receipt and message_or_receipt["protocol"] == "CONTROLLER_RELAY_V1":
        return message_or_receipt.get("authority_effect") == "NONE"
    if "protocol" in message_or_receipt and message_or_receipt["protocol"] == "CONTROLLER_RELAY_RECEIPT_V1":
        # Receipts have no authority_effect property at all
        return "authority_effect" not in message_or_receipt
    return False
