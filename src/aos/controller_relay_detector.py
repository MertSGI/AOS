"""AOS Controller Relay CR2-Lite Inbound Detector.

Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01

Implements the exact detection algorithm:
- Reads Relay HEAD via ControllerRelayService.
- Validates bootstrap ancestry and complete immutable history.
- Validates message and receipt schemas and canonical hashes.
- Validates sequence ordering and thread/in_reply_to binding.
- Applies supersession rules (superseded messages are excluded).
- Filters to target Controller (messages directed TO target_controller).
- Excludes valid consumed lifecycle state.
- Prohibits CONSUMED progression for requires_reply=True if no real qualifying reply exists.
- Selects latest unconsumed inbound by actual Git publication ordinal and valid directed sequence.
- NEVER uses filename lexical order alone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from aos.controller_relay import (
    ControllerRelayError,
    ControllerRelayValidationResult,
)
from aos.controller_relay_service import ControllerRelayService


class UnconsumedInboundMessage:
    """Represents a validated, unconsumed inbound Relay message."""

    def __init__(
        self,
        message: Dict[str, Any],
        publication_commit_sha: str,
        path: str,
        publication_ordinal: int,
    ):
        self.message = message
        self.publication_commit_sha = publication_commit_sha
        self.path = path
        self.publication_ordinal = publication_ordinal

    @property
    def message_id(self) -> str:
        return self.message.get("message_id", "")

    @property
    def thread_id(self) -> str:
        return self.message.get("thread_id", "")

    @property
    def from_controller(self) -> str:
        return self.message.get("from", "")

    @property
    def to_controller(self) -> str:
        return self.message.get("to", "")

    @property
    def sequence(self) -> int:
        return int(self.message.get("sequence", 0))

    @property
    def requires_reply(self) -> bool:
        return bool(self.message.get("requires_reply", False))

    @property
    def authority_effect(self) -> str:
        return self.message.get("authority_effect", "NONE")

    def __repr__(self) -> str:
        return (
            f"UnconsumedInboundMessage(message_id={self.message_id!r}, "
            f"from={self.from_controller!r}, to={self.to_controller!r}, "
            f"seq={self.sequence}, ord={self.publication_ordinal})"
        )


def detect_unconsumed_inbound_messages(
    service: ControllerRelayService,
    target_controller: str,
    expected_head: Optional[str] = None,
) -> List[UnconsumedInboundMessage]:
    """Detect all validated, non-superseded, unconsumed inbound messages for target_controller.

    Returns list ordered by actual publication ordinal and valid directed sequence.
    Fails closed on any history corruption or lifecycle invalidity.
    """
    head_sha = expected_head if expected_head else service.get_head()

    # 1. Complete immutable history and lifecycle validation across trusted lineage
    msg_map, rcpt_entries, val_res = service._validate_existing_relay_history(head_sha)
    if not val_res.is_valid:
        raise ControllerRelayError(
            f"Failed relay history validation during inbound detection: {val_res.disposition}: {val_res.errors}"
        )

    # 2. Determine all superseded message IDs
    superseded_message_ids: set[str] = set()
    for entry in msg_map.values():
        msg = entry["message"]
        s_id = msg.get("supersedes_message_id")
        if s_id:
            superseded_message_ids.add(s_id)

    # 3. Determine consumed message IDs
    consumed_message_ids: set[str] = set()
    for entry in rcpt_entries:
        rcpt = entry["receipt"]
        if rcpt.get("event") == "CONSUMED":
            consumed_message_ids.add(rcpt.get("message_id", ""))

    # 4. Filter candidate inbound messages for target_controller
    candidates: List[Dict[str, Any]] = []
    for msg_id, entry in msg_map.items():
        msg = entry["message"]
        if msg.get("to") != target_controller:
            continue
        if msg_id in superseded_message_ids:
            continue
        if msg_id in consumed_message_ids:
            continue
        candidates.append(entry)

    # 5. Sort candidate messages by actual Git publication ordinal and sequence
    candidates.sort(key=lambda e: (e["publication_ordinal"], int(e["message"].get("sequence", 0))))

    return [
        UnconsumedInboundMessage(
            message=c["message"],
            publication_commit_sha=c["publication_commit_sha"],
            path=c["path"],
            publication_ordinal=c["publication_ordinal"],
        )
        for c in candidates
    ]


def get_latest_unconsumed_inbound(
    service: ControllerRelayService,
    target_controller: str,
    expected_head: Optional[str] = None,
) -> Optional[UnconsumedInboundMessage]:
    """Get the latest validated unconsumed inbound message for target_controller.

    Returns None if no unconsumed inbound messages exist.
    """
    unconsumed = detect_unconsumed_inbound_messages(
        service=service,
        target_controller=target_controller,
        expected_head=expected_head,
    )
    if not unconsumed:
        return None
    return unconsumed[-1]
