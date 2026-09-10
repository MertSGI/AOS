"""AOS Controller Relay CR2-Lite Active-Runtime Polling Coordinator.

Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01

Implements CR2-lite active-runtime polling:
- Startup check
- Before every cross-controller action
- Immediately after publish
- While awaiting expected response
- Default/recommended poll interval: 30.0 seconds
- HEAD comparison caching to avoid unnecessary full history traversal when HEAD unchanged
- Any HEAD change triggers full accepted-history validation and latest-unconsumed detection
- Zero hosted workflow dispatch, zero persistent background daemon under this authority
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.controller_relay_detector import (
    UnconsumedInboundMessage,
    detect_unconsumed_inbound_messages,
    get_latest_unconsumed_inbound,
)
from aos.controller_relay_service import ControllerRelayService

DEFAULT_POLL_INTERVAL_SECONDS: float = 30.0


class ControllerRelayPoller:
    """Bounded, stateful polling coordinator for Controller Relay active sessions."""

    def __init__(
        self,
        service: ControllerRelayService,
        controller_id: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ):
        self._service = service
        self._controller_id = controller_id
        self._poll_interval = poll_interval
        self._clock = clock if clock is not None else time.time
        self._sleeper = sleeper if sleeper is not None else time.sleep

        self._last_observed_head: Optional[str] = None
        self._last_unconsumed: Optional[UnconsumedInboundMessage] = None
        self._poll_count: int = 0

    @property
    def controller_id(self) -> str:
        return self._controller_id

    @property
    def last_observed_head(self) -> Optional[str]:
        return self._last_observed_head

    @property
    def poll_count(self) -> int:
        return self._poll_count

    def poll_once(self) -> Tuple[bool, Optional[UnconsumedInboundMessage]]:
        """Perform a single bounded check against Relay HEAD.

        Returns (head_changed: bool, latest_unconsumed: Optional[UnconsumedInboundMessage]).
        If HEAD has not changed since last check, returns (False, cached_unconsumed) without re-validating history.
        If HEAD changed, triggers full immutable history validation and detects latest unconsumed.
        """
        self._poll_count += 1
        current_head = self._service.get_head()

        if current_head == self._last_observed_head:
            return False, self._last_unconsumed

        # Head changed (or first poll): full validation and fresh detection
        latest = get_latest_unconsumed_inbound(
            service=self._service,
            target_controller=self._controller_id,
            expected_head=current_head,
        )

        self._last_observed_head = current_head
        self._last_unconsumed = latest
        return True, latest

    def wait_for_inbound(
        self,
        max_attempts: int = 10,
        expected_thread_id: Optional[str] = None,
        expected_in_reply_to: Optional[str] = None,
    ) -> Optional[UnconsumedInboundMessage]:
        """Poll iteratively with interval until a matching unconsumed message arrives or attempts expire.

        Useful for bounded waiting after publication without persistent daemon.
        """
        for attempt in range(max_attempts):
            changed, latest = self.poll_once()
            if latest is not None:
                matches_thread = expected_thread_id is None or latest.thread_id == expected_thread_id
                matches_reply = (
                    expected_in_reply_to is None
                    or latest.message.get("in_reply_to") == expected_in_reply_to
                )
                if matches_thread and matches_reply:
                    return latest

            if attempt < max_attempts - 1:
                self._sleeper(self._poll_interval)

        return None
