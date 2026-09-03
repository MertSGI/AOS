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
    ControllerRelayError,
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

    def list_message_provenances(self, ref: Optional[str] = None) -> List[Tuple[Dict[str, Any], str]]:
        """Fetch, raw-validate, and return (message_dict, publication_commit_sha) tuples from target ref.

        Fails closed on any invalid, malformed, BOM, or schema-failing historical message record.
        """
        target_ref = ref if ref else self._branch
        msg_map, _, val_res = self._validate_existing_relay_history(target_ref)
        if not val_res.is_valid:
            raise ControllerRelayError(f"{val_res.disposition}: {val_res.errors}")
        return [(entry["message"], entry["publication_commit_sha"]) for entry in msg_map.values()]

    def list_messages(self, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch and parse all existing Relay messages from target ref. Fail closed if any record is invalid."""
        return [msg for msg, _ in self.list_message_provenances(ref=ref)]

    def list_receipt_provenances(self, ref: Optional[str] = None) -> List[Tuple[Dict[str, Any], str]]:
        """Fetch, raw-validate, and return (receipt_dict, publication_commit_sha) tuples from target ref.

        Fails closed on any invalid, malformed, BOM, or schema-failing historical receipt record.
        """
        target_ref = ref if ref else self._branch
        _, rcpt_entries, val_res = self._validate_existing_relay_history(target_ref)
        if not val_res.is_valid:
            raise ControllerRelayError(f"{val_res.disposition}: {val_res.errors}")
        return [(entry["receipt"], entry["publication_commit_sha"]) for entry in rcpt_entries]

    def list_receipts(self, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch and parse all existing Relay receipts from target ref. Fail closed if any record is invalid."""
        return [rcpt for rcpt, _ in self.list_receipt_provenances(ref=ref)]

    def _validate_existing_relay_history(
        self, expected_head: str
    ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], ControllerRelayValidationResult]:
        """Single global reusable fail-closed history validator across trusted lineage.

        Returns (message_map, receipt_entries, validation_result).
        message_map: msg_id -> { "message": dict, "publication_commit_sha": str, "path": str, "publication_ordinal": int }
        receipt_entries: list of { "receipt": dict, "publication_commit_sha": str, "path": str, "publication_ordinal": int }
        """
        message_map: Dict[str, Dict[str, Any]] = {}
        receipt_entries: List[Dict[str, Any]] = []

        # 1. Fetch provenances over trusted lineage via transport
        try:
            msg_provenances = self._transport.list_record_provenance_under_prefix(
                self._repository, expected_head, "controller-relay/v1/messages/"
            )
            rcpt_provenances = self._transport.list_record_provenance_under_prefix(
                self._repository, expected_head, "controller-relay/v1/receipts/"
            )
        except Exception as exc:
            err_msg = str(exc)
            disposition = "HOLD_INVALID_RELAY_HISTORY"
            for known_disp in [
                "HOLD_RELAY_BOOTSTRAP_UNREACHABLE",
                "HOLD_RELAY_RECORD_REAPPEARED",
                "HOLD_RELAY_RECORD_MUTATED",
                "HOLD_RECORD_PROVENANCE_UNVERIFIABLE",
            ]:
                if known_disp in err_msg:
                    disposition = known_disp
                    break
            return (
                {},
                [],
                ControllerRelayValidationResult(
                    False,
                    disposition,
                    [err_msg],
                    details={
                        "GIT_OBJECT_CREATE_COUNT": 0,
                        "REF_UPDATE_COUNT": 0,
                        "TRANSPORT_PUBLICATION_COUNT": 0,
                    },
                ),
            )

        # 2. Process historical messages
        for prov in msg_provenances:
            path, raw_bytes, pub_sha, pub_ord = prov.path, prov.raw_bytes, prov.publication_commit_sha, prov.publication_ordinal
            raw_res = validate_controller_relay_message_raw(raw_bytes)
            if not raw_res.is_valid:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Raw message validation failed at '{path}': {raw_res.errors}"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            msg_dict, _ = _parse_raw_relay_bytes_strict(raw_bytes)
            if not isinstance(msg_dict, dict):
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Parsed payload at '{path}' is not a dict"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            msg_id = msg_dict.get("message_id")
            if not msg_id or msg_id in message_map:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Duplicate or missing message_id '{msg_id}' at path '{path}'"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            message_map[msg_id] = {
                "message": msg_dict,
                "publication_commit_sha": pub_sha,
                "path": path,
                "publication_ordinal": pub_ord,
            }

        # 3. Process historical receipts
        for prov in rcpt_provenances:
            path, raw_bytes, pub_sha, pub_ord = prov.path, prov.raw_bytes, prov.publication_commit_sha, prov.publication_ordinal
            raw_res = validate_controller_relay_receipt_raw(raw_bytes)
            if not raw_res.is_valid:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Raw receipt validation failed at '{path}': {raw_res.errors}"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            rcpt_dict, _ = _parse_raw_relay_bytes_strict(raw_bytes)
            if not isinstance(rcpt_dict, dict):
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Parsed receipt payload at '{path}' is not a dict"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            target_msg_id = rcpt_dict.get("message_id")
            if target_msg_id not in message_map:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Receipt at '{path}' references unknown or non-existent message_id '{target_msg_id}'"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )
            target_entry = message_map[target_msg_id]
            target_msg = target_entry["message"]

            # Content hash binding check
            if rcpt_dict.get("message_content_sha256") != target_msg.get("content_sha256"):
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Receipt at '{path}' message_content_sha256 mismatch for '{target_msg_id}'"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )

            # Publication commit binding check
            if rcpt_dict.get("message_commit_sha") != target_entry["publication_commit_sha"]:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [
                            f"Receipt at '{path}' message_commit_sha mismatch for '{target_msg_id}': "
                            f"claimed '{rcpt_dict.get('message_commit_sha')}', actual '{target_entry['publication_commit_sha']}'"
                        ],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )

            receipt_entries.append({
                "receipt": rcpt_dict,
                "publication_commit_sha": pub_sha,
                "path": path,
                "publication_ordinal": pub_ord,
            })

        # 4. Message sequence, thread, and conflict checks
        sorted_messages = [e["message"] for e in sorted(message_map.values(), key=lambda x: x["publication_ordinal"])]
        sorted_raw_receipts = [e["receipt"] for e in sorted(receipt_entries, key=lambda x: x["publication_ordinal"])]

        seq_res = validate_channel_sequence_history(sorted_messages)
        if not seq_res.is_valid:
            return (
                {},
                [],
                ControllerRelayValidationResult(
                    False,
                    "HOLD_INVALID_RELAY_HISTORY",
                    seq_res.errors,
                    details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                ),
            )

        thread_res = validate_thread_history(sorted_messages)
        if not thread_res.is_valid:
            return (
                {},
                [],
                ControllerRelayValidationResult(
                    False,
                    "HOLD_INVALID_RELAY_HISTORY",
                    thread_res.errors,
                    details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                ),
            )

        conflict_res = detect_relay_conflicts_and_replays(sorted_messages, sorted_raw_receipts)
        if not conflict_res.is_valid:
            return (
                {},
                [],
                ControllerRelayValidationResult(
                    False,
                    "HOLD_INVALID_RELAY_HISTORY",
                    conflict_res.errors,
                    details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                ),
            )

        # 5. Global receipt lifecycle & temporal reply validation for EVERY historical message
        for msg_id, msg_entry in message_map.items():
            msg = msg_entry["message"]
            msg_rcpt_entries = sorted(
                [r for r in receipt_entries if r["receipt"].get("message_id") == msg_id],
                key=lambda x: x["publication_ordinal"],
            )
            msg_rcpts = [r["receipt"] for r in msg_rcpt_entries]

            # Check requires_reply temporal integrity for CONSUMED receipts
            if msg.get("requires_reply") is True:
                for r_entry in msg_rcpt_entries:
                    if r_entry["receipt"].get("event") == "CONSUMED":
                        consumed_ord = r_entry["publication_ordinal"]
                        qualifying_reply = next(
                            (
                                candidate["message"]
                                for candidate in message_map.values()
                                if candidate["message"].get("message_id") != msg_id
                                and candidate["message"].get("in_reply_to") == msg_id
                                and candidate["message"].get("thread_id") == msg.get("thread_id")
                                and candidate["message"].get("from") == msg.get("to")
                                and candidate["message"].get("to") == msg.get("from")
                                and candidate["publication_ordinal"] < consumed_ord
                            ),
                            None,
                        )
                        if not qualifying_reply:
                            return (
                                {},
                                [],
                                ControllerRelayValidationResult(
                                    False,
                                    "HOLD_INVALID_RELAY_HISTORY",
                                    [
                                        f"Historical CONSUMED receipt for message '{msg_id}' requiring reply "
                                        f"was published at ordinal {consumed_ord} before any qualifying reply was published"
                                    ],
                                    details={
                                        "GIT_OBJECT_CREATE_COUNT": 0,
                                        "REF_UPDATE_COUNT": 0,
                                        "TRANSPORT_PUBLICATION_COUNT": 0,
                                    },
                                ),
                            )

            # Validate lifecycle with qualifying replies available prior to receipts
            outbound_replies = [
                cand["message"]
                for cand in message_map.values()
                if cand["message"].get("message_id") != msg_id
                and cand["message"].get("in_reply_to") == msg_id
                and cand["message"].get("thread_id") == msg.get("thread_id")
                and cand["message"].get("from") == msg.get("to")
                and cand["message"].get("to") == msg.get("from")
            ]
            lc_res = validate_receipt_lifecycle(msg, msg_rcpts, outbound_replies=outbound_replies)
            if not lc_res.is_valid:
                return (
                    {},
                    [],
                    ControllerRelayValidationResult(
                        False,
                        "HOLD_INVALID_RELAY_HISTORY",
                        [f"Lifecycle validation failed for message '{msg_id}': {lc_res.errors}"],
                        details={"GIT_OBJECT_CREATE_COUNT": 0, "REF_UPDATE_COUNT": 0, "TRANSPORT_PUBLICATION_COUNT": 0},
                    ),
                )

        return (message_map, receipt_entries, ControllerRelayValidationResult(True, "PASS"))

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
        4. Validate existing relay history over single first-parent lineage.
        5. Validate sequence, thread, and conflict invariants.
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
        msg_map, rcpt_entries, hist_res = self._validate_existing_relay_history(expected_head)
        if not hist_res.is_valid:
            return hist_res

        existing_messages = [e["message"] for e in msg_map.values()]
        existing_receipts = [e["receipt"] for e in rcpt_entries]
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
        4. Validate existing relay history over single first-parent lineage.
        5. Resolve target message and verify content-hash and publication-commit binding.
        6. Load existing receipt history for message and validate lifecycle progression.
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

        # 5. Existing history validation
        msg_map, rcpt_entries, hist_res = self._validate_existing_relay_history(expected_head)
        if not hist_res.is_valid:
            return hist_res

        # 6. Resolve target message & publication commit
        target_msg_id = receipt.get("message_id")
        target_entry = msg_map.get(target_msg_id)

        if not target_entry:
            return ControllerRelayValidationResult(
                False,
                "HOLD_INVALID_RELAY_HISTORY",
                [f"Receipt targets non-existent or unknown message_id '{target_msg_id}'"],
                details={
                    "GIT_OBJECT_CREATE_COUNT": 0,
                    "REF_UPDATE_COUNT": 0,
                    "TRANSPORT_PUBLICATION_COUNT": 0,
                },
            )

        target_message = target_entry["message"]
        pub_commit_sha = target_entry["publication_commit_sha"]

        # 7. Content hash binding
        rcpt_hash = receipt.get("message_content_sha256")
        msg_hash = target_message.get("content_sha256")
        if rcpt_hash != msg_hash:
            return ControllerRelayValidationResult(
                False,
                "HOLD_INVALID_RELAY_HISTORY",
                [
                    f"Receipt content_sha256 mismatch for message '{target_msg_id}': "
                    f"got '{rcpt_hash}', expected '{msg_hash}'"
                ],
                details={
                    "GIT_OBJECT_CREATE_COUNT": 0,
                    "REF_UPDATE_COUNT": 0,
                    "TRANSPORT_PUBLICATION_COUNT": 0,
                },
            )

        # 8. Message commit binding
        rcpt_commit_sha = receipt.get("message_commit_sha")
        if rcpt_commit_sha != pub_commit_sha:
            return ControllerRelayValidationResult(
                False,
                "HOLD_RECEIPT_MESSAGE_COMMIT_MISMATCH",
                [
                    f"Receipt message_commit_sha mismatch for message '{target_msg_id}': "
                    f"receipt claimed '{rcpt_commit_sha}', actual publication commit '{pub_commit_sha}'"
                ],
                details={
                    "GIT_OBJECT_CREATE_COUNT": 0,
                    "REF_UPDATE_COUNT": 0,
                    "TRANSPORT_PUBLICATION_COUNT": 0,
                },
            )

        # 9. Receipt lifecycle check
        existing_receipts = [e["receipt"] for e in rcpt_entries]
        msg_receipts = [r for r in existing_receipts if r.get("message_id") == target_msg_id]
        all_msg_receipts = msg_receipts + [receipt]

        outbound_replies = [
            cand["message"]
            for cand in msg_map.values()
            if cand["message"].get("message_id") != target_msg_id
            and cand["message"].get("in_reply_to") == target_msg_id
            and cand["message"].get("thread_id") == target_message.get("thread_id")
            and cand["message"].get("from") == target_message.get("to")
            and cand["message"].get("to") == target_message.get("from")
        ]

        lifecycle_res = validate_receipt_lifecycle(target_message, all_msg_receipts, outbound_replies=outbound_replies)
        if not lifecycle_res.is_valid:
            return lifecycle_res

        # 10. Transport CAS mutation
        return self._transport.publish_record(
            repository=self._repository,
            branch=self._branch,
            path=derived_path,
            content_bytes=raw_bytes,
            expected_head=expected_head,
            record_type="receipt",
        )

