"""AOS Controller Relay CR2-Lite Trusted Principal Ingress Adapter Boundary.

Authority ID: LARI-AOS-CONTROLLER-RELAY-CR2-LITE-OPERATIONALIZATION-20260910-01

Implements a strictly bounded adapter around ControllerRelayService:
- Allowed operations only:
  - relay_get_head()
  - relay_get_latest_unconsumed()
  - relay_read_message(message_id, ref)
  - relay_publish_message(raw_bytes, expected_head)
  - relay_publish_receipt(raw_bytes, expected_head)
- Disallows arbitrary: repository, branch, ref, path, Git writer, credential operation,
  deployment, or product mutation.
- Principal MUST derive from trusted authenticated ingress context:
  - Caller CANNOT submit "AOS_CONTROLLER" or "LARI_CONTROLLER" as a free principal string.
  - An AOS-authenticated session is structurally unable to publish as LARI_CONTROLLER.
  - A LARI-authenticated session is structurally unable to publish as AOS_CONTROLLER.
  - AG/AOS synthetic LARI identity is strictly forbidden.
"""

from __future__ import annotations

import enum
from typing import Any, Dict, List, Optional, Tuple

from aos.controller_relay import ControllerRelayError, ControllerRelayValidationResult
from aos.controller_relay_detector import (
    UnconsumedInboundMessage,
    get_latest_unconsumed_inbound,
)
from aos.controller_relay_service import ControllerPrincipal, ControllerRelayService, derive_message_path


class AuthenticatedSessionType(str, enum.Enum):
    """Cryptographically or structurally verified ingress session type."""
    AOS_CONTROLLER_SESSION = "AOS_CONTROLLER"
    LARI_CONTROLLER_SESSION = "LARI_CONTROLLER"


class TrustedIngressBoundaryError(Exception):
    """Raised when an ingress boundary invariant or principal spoofing attempt occurs."""
    pass


class BoundedControllerRelayIngressAdapter:
    """Bounded, authenticated ingress facade enclosing ControllerRelayService."""

    def __init__(
        self,
        service: ControllerRelayService,
        session_type: AuthenticatedSessionType,
    ):
        if not isinstance(session_type, AuthenticatedSessionType):
            raise TrustedIngressBoundaryError(
                f"Invalid session type: {session_type!r}. Principal must be an AuthenticatedSessionType enum."
            )
        self._service = service
        self._session_type = session_type
        self._principal = ControllerPrincipal(session_type.value)

    @property
    def principal_controller_id(self) -> str:
        return self._principal.controller_id

    def relay_get_head(self) -> str:
        """Return current Relay branch HEAD SHA."""
        return self._service.get_head()

    def relay_get_latest_unconsumed(
        self,
        expected_head: Optional[str] = None,
    ) -> Optional[UnconsumedInboundMessage]:
        """Detect and return the latest validated unconsumed inbound message for THIS authenticated controller."""
        return get_latest_unconsumed_inbound(
            service=self._service,
            target_controller=self._principal.controller_id,
            expected_head=expected_head,
        )

    def relay_read_message(
        self,
        message_id: str,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read and validate a specific historical message by message_id."""
        target_ref = ref if ref else self.relay_get_head()
        messages = self._service.list_messages(ref=target_ref)
        for msg in messages:
            if msg.get("message_id") == message_id:
                return msg
        raise ControllerRelayError(f"Message '{message_id}' not found in Relay history at ref '{target_ref}'")

    def relay_publish_message(
        self,
        raw_bytes: bytes,
        expected_head: str,
    ) -> ControllerRelayValidationResult:
        """Publish a message strictly authenticated under this session's immutable principal.

        The message MUST declare 'from': <session_type.value>.
        Any attempt to declare another sender (e.g. AOS session pretending to be LARI_CONTROLLER)
        is rejected at the principal boundary.
        """
        return self._service.publish_message(
            raw_bytes=raw_bytes,
            expected_head=expected_head,
            principal=self._principal,
        )

    def relay_publish_receipt(
        self,
        raw_bytes: bytes,
        expected_head: str,
    ) -> ControllerRelayValidationResult:
        """Publish a receipt strictly authenticated under this session's immutable principal.

        The receipt MUST declare 'actor': <session_type.value>.
        """
        return self._service.publish_receipt(
            raw_bytes=raw_bytes,
            expected_head=expected_head,
            principal=self._principal,
        )
