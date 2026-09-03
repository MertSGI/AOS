"""AOS Controller Relay Service V1 Network-Neutral Core Implementation.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-SERVICE-V1-FOUNDATION-20260903-01

This module provides the ControllerRelayService and ControllerPrincipal abstractions.
It enforces:
- Trusted authenticated principal boundaries (ControllerPrincipal)
- Path derivation rules and path safety check
- Pre-mutation protocol, history, thread, and receipt lifecycle validation
- Invariant bounds (fixed repository MertSGI/AOS, fixed branch control/controller-relay)
- Delegation to canonical protocol engine (aos.controller_relay)
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from aos.controller_relay import (
    CONTROLLER_ID_REGEX,
    MESSAGE_ID_REGEX,
    ControllerRelayValidationResult,
    _parse_raw_relay_bytes_strict,
    detect_relay_conflicts_and_replays,
    validate_channel_sequence_history,
    validate_controller_relay_message_raw,
    validate_controller_relay_receipt_raw,
    validate_receipt_lifecycle,
    validate_thread_history,
)

FIXED_RELAY_REPOSITORY: str = "MertSGI/AOS"
FIXED_RELAY_BRANCH: str = "control/controller-relay"
FIXED_RELAY_REF: str = "refs/heads/control/controller-relay"

SAFE_PATH_COMPONENT_REGEX = re.compile(r"^[A-Za-z0-9_\-]+$")


class ControllerPrincipal:
    """Authenticated identity established OUTSIDE Relay JSON payload.

    A Relay record's message.from or receipt.actor is self-declared metadata, NOT authentication.
    The principal must be established prior to service calls (e.g., injected by future MCP/HTTP adapters).
    """

    def __init__(self, controller_id: str):
        if not isinstance(controller_id, str) or not CONTROLLER_ID_REGEX.match(controller_id):
            raise ValueError(f"Invalid controller_id for ControllerPrincipal: '{controller_id}'")
        self._controller_id = controller_id

    @property
    def controller_id(self) -> str:
        return self._controller_id

    def __repr__(self) -> str:
        return f"ControllerPrincipal(controller_id={self._controller_id!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ControllerPrincipal):
            return self._controller_id == other._controller_id
        return False


def derive_message_path(message: Dict[str, Any]) -> str:
    """Derive internal message storage path.

    Format: controller-relay/v1/messages/<FROM>--<TO>/<SEQUENCE_12>-<MESSAGE_ID>.json
    Caller CANNOT override path.
    """
    from_c = message.get("from", "")
    to_c = message.get("to", "")
    msg_id = message.get("message_id", "")
    seq = message.get("sequence", 0)

    if not SAFE_PATH_COMPONENT_REGEX.match(from_c):
        raise ValueError(f"Malformed from controller component in path derivation: '{from_c}'")
    if not SAFE_PATH_COMPONENT_REGEX.match(to_c):
        raise ValueError(f"Malformed to controller component in path derivation: '{to_c}'")
    if not SAFE_PATH_COMPONENT_REGEX.match(msg_id):
        raise ValueError(f"Malformed message_id component in path derivation: '{msg_id}'")
    if not isinstance(seq, int) or seq < 1 or seq > 999999999999:
        raise ValueError(f"Invalid sequence component in path derivation: {seq}")

    return f"controller-relay/v1/messages/{from_c}--{to_c}/{seq:012d}-{msg_id}.json"


def derive_receipt_path(receipt: Dict[str, Any]) -> str:
    """Derive internal receipt storage path.

    Format: controller-relay/v1/receipts/<MESSAGE_ID>/<ACTOR>-<EVENT>.json
    Caller CANNOT override path.
    """
    msg_id = receipt.get("message_id", "")
    actor = receipt.get("actor", "")
    event = receipt.get("event", "")

    if not SAFE_PATH_COMPONENT_REGEX.match(msg_id):
        raise ValueError(f"Malformed message_id component in receipt path derivation: '{msg_id}'")
    if not SAFE_PATH_COMPONENT_REGEX.match(actor):
        raise ValueError(f"Malformed actor component in receipt path derivation: '{actor}'")
    if not SAFE_PATH_COMPONENT_REGEX.match(event):
        raise ValueError(f"Malformed event component in receipt path derivation: '{event}'")

    return f"controller-relay/v1/receipts/{msg_id}/{actor}-{event}.json"


class ControllerRelayService:
    """Network-neutral AOS Controller Relay Service V1 core engine.

    Handles protocol validation, principal authentication, path derivation,
    and history checks before delegating Git object creation to transport.
    """

    def __init__(self, transport: Any):
        """Initialize ControllerRelayService with an injected Git CAS transport."""
        self._transport = transport
        self._repository = FIXED_RELAY_REPOSITORY
        self._branch = FIXED_RELAY_BRANCH

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def branch(self) -> str:
        return self._branch

    def get_head(self) -> str:
        """Get current HEAD SHA of control/controller-relay branch."""
        return self._transport.get_branch_head(self._repository, self._branch)

    def read_record(self, path: str, ref: Optional[str] = None) -> bytes:
        """Read a Relay record content from target path and ref."""
        target_ref = ref if ref else self._branch
        return self._transport.read_record_bytes(self._repository, target_ref, path)

    def list_messages(self, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch and parse all existing Relay messages from target ref."""
        target_ref = ref if ref else self._branch
        raw_records = self._transport.list_records_under_prefix(
            self._repository, target_ref, "controller-relay/v1/messages/"
        )
        messages: List[Dict[str, Any]] = []
        for _, raw_bytes in raw_records:
            try:
                data, errs = _parse_raw_relay_bytes_strict(raw_bytes)
                if not errs and isinstance(data, dict):
                    messages.append(data)
            except Exception:
                continue
        return messages

    def list_receipts(self, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch and parse all existing Relay receipts from target ref."""
        target_ref = ref if ref else self._branch
        raw_records = self._transport.list_records_under_prefix(
            self._repository, target_ref, "controller-relay/v1/receipts/"
        )
        receipts: List[Dict[str, Any]] = []
        for _, raw_bytes in raw_records:
            try:
                data, errs = _parse_raw_relay_bytes_strict(raw_bytes)
                if not errs and isinstance(data, dict):
                    receipts.append(data)
            except Exception:
                continue
        return receipts

    def publish_message(
        self,
        raw_bytes: bytes,
        expected_head: str,
        principal: ControllerPrincipal,
    ) -> ControllerRelayValidationResult:
        """Validate and publish a new Relay message.

        Execution Pipeline:
        1. Validate raw message bytes (schema, canonical hash, authority_effect="NONE", secrets).
        2. Authenticate principal: principal.controller_id MUST equal message.from.
        3. Derive storage path internally.
        4. Load existing channel history and validate sequence, thread, and conflict invariants.
        5. Ensure target immutable path is absent in tree.
        6. Delegate CAS publication to transport.
        """
        # 1. Raw message validation
        val_res = validate_controller_relay_message_raw(raw_bytes)
        if not val_res.is_valid:
            return val_res

        # 2. Parse payload dict
        message, _ = _parse_raw_relay_bytes_strict(raw_bytes)
        if not isinstance(message, dict):
            return ControllerRelayValidationResult(False, "FAIL", ["Message payload is not a valid JSON object"])

        # 3. Principal authentication boundary
        msg_from = message.get("from")
        if principal.controller_id != msg_from:
            return ControllerRelayValidationResult(
                False,
                "FAIL",
                [
                    f"Authentication mismatch: principal controller_id ('{principal.controller_id}') "
                    f"does not match message.from ('{msg_from}')"
                ],
            )

        # 4. Internal path derivation
        try:
            derived_path = derive_message_path(message)
        except Exception as exc:
            return ControllerRelayValidationResult(False, "FAIL", [f"Path derivation failed: {exc}"])

        # 5. Existing history validation
        existing_messages = self.list_messages(ref=expected_head)
        all_messages = existing_messages + [message]

        # Directed sequence check
        seq_res = validate_channel_sequence_history(all_messages)
        if not seq_res.is_valid:
            return seq_res

        # Thread history check
        thread_res = validate_thread_history(all_messages)
        if not thread_res.is_valid:
            return thread_res

        # Conflicts and replays check
        existing_receipts = self.list_receipts(ref=expected_head)
        conflict_res = detect_relay_conflicts_and_replays(all_messages, existing_receipts)
        if not conflict_res.is_valid:
            return conflict_res

        # 6. Transport CAS mutation
        return self._transport.publish_record(
            repository=self._repository,
            branch=self._branch,
            path=derived_path,
            content_bytes=raw_bytes,
            expected_head=expected_head,
            record_type="message",
        )

    def publish_receipt(
        self,
        raw_bytes: bytes,
        expected_head: str,
        principal: ControllerPrincipal,
    ) -> ControllerRelayValidationResult:
        """Validate and publish a new Relay receipt.

        Execution Pipeline:
        1. Validate raw receipt bytes (schema, secrets, protocol).
        2. Authenticate principal: principal.controller_id MUST equal receipt.actor.
        3. Derive storage path internally.
        4. Resolve target message and verify content-hash binding.
        5. Load existing receipt history for message and validate lifecycle progression.
        6. Ensure target immutable path is absent in tree.
        7. Delegate CAS publication to transport.
        """
        # 1. Raw receipt validation
        val_res = validate_controller_relay_receipt_raw(raw_bytes)
        if not val_res.is_valid:
            return val_res

        # 2. Parse payload dict
        receipt, _ = _parse_raw_relay_bytes_strict(raw_bytes)
        if not isinstance(receipt, dict):
            return ControllerRelayValidationResult(False, "FAIL", ["Receipt payload is not a valid JSON object"])

        # 3. Principal authentication boundary
        rcpt_actor = receipt.get("actor")
        if principal.controller_id != rcpt_actor:
            return ControllerRelayValidationResult(
                False,
                "FAIL",
                [
                    f"Authentication mismatch: principal controller_id ('{principal.controller_id}') "
                    f"does not match receipt.actor ('{rcpt_actor}')"
                ],
            )

        # 4. Internal path derivation
        try:
            derived_path = derive_receipt_path(receipt)
        except Exception as exc:
            return ControllerRelayValidationResult(False, "FAIL", [f"Receipt path derivation failed: {exc}"])

        # 5. Resolve target message
        target_msg_id = receipt.get("message_id")
        existing_messages = self.list_messages(ref=expected_head)
        target_message = next((m for m in existing_messages if m.get("message_id") == target_msg_id), None)

        if not target_message:
            return ControllerRelayValidationResult(
                False, "FAIL", [f"Receipt targets non-existent or unknown message_id '{target_msg_id}'"]
            )

        # 6. Content hash binding
        rcpt_hash = receipt.get("message_content_sha256")
        msg_hash = target_message.get("content_sha256")
        if rcpt_hash != msg_hash:
            return ControllerRelayValidationResult(
                False,
                "FAIL",
                [
                    f"Receipt content_sha256 mismatch for message '{target_msg_id}': "
                    f"got '{rcpt_hash}', expected '{msg_hash}'"
                ],
            )

        # 7. Receipt lifecycle check
        existing_receipts = self.list_receipts(ref=expected_head)
        msg_receipts = [r for r in existing_receipts if r.get("message_id") == target_msg_id]
        all_msg_receipts = msg_receipts + [receipt]

        # Determine outbound replies for CONSUMED validation if required
        outbound_replies = [
            m for m in existing_messages if m.get("in_reply_to") == target_msg_id or m.get("thread_id") == target_message.get("thread_id")
        ]

        lifecycle_res = validate_receipt_lifecycle(target_message, all_msg_receipts, outbound_replies=outbound_replies)
        if not lifecycle_res.is_valid:
            return lifecycle_res

        # 8. Transport CAS mutation
        return self._transport.publish_record(
            repository=self._repository,
            branch=self._branch,
            path=derived_path,
            content_bytes=raw_bytes,
            expected_head=expected_head,
            record_type="receipt",
        )
