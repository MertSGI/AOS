"""AOS Controller Relay V1 Identity & Credential Provider Implementation.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-20260903-01

Provides:
- InjectedInstallationTokenCredentialProvider: Memory-only short-lived GitHub App installation token provider
- Strict 120-second expiry safety boundary
- Zero environment, PAT, OAuth, file, or keychain credential discovery
- Token redaction from repr/str and exception outputs
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from aos.controller_relay_git_transport import ControllerRelayTransportError, CredentialProvider

EXPIRY_SAFETY_MARGIN_SECONDS: float = 120.0


class InjectedInstallationTokenCredentialProvider(CredentialProvider):
    """Memory-only short-lived GitHub App installation token provider.

    Accepts an already generated short-lived installation access token injected by an external,
    trusted caller. Performs zero credential discovery, environment lookup, or file reads.
    """

    def __init__(
        self,
        token: str,
        expires_at: datetime,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("Installation token MUST be a non-empty string")

        if not isinstance(expires_at, datetime):
            raise ValueError("expires_at MUST be a valid datetime instance")

        if expires_at.tzinfo is None or expires_at.tzinfo.utcoffset(expires_at) is None:
            raise ValueError("expires_at MUST be an explicit timezone-aware UTC datetime")

        self._token = token
        self._expires_at = expires_at.astimezone(timezone.utc)
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        exp_iso = self._expires_at.isoformat()
        return f"InjectedInstallationTokenCredentialProvider(token='***REDACTED***', expires_at='{exp_iso}')"

    def __str__(self) -> str:
        exp_iso = self._expires_at.isoformat()
        return f"InjectedInstallationTokenCredentialProvider(token='***REDACTED***', expires_at='{exp_iso}')"

    def get_token(self) -> str:
        """Retrieve the injected bearer token, asserting strict 120-second lifetime safety margin."""
        now = self._clock()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        remaining_seconds = (self._expires_at - now).total_seconds()
        if remaining_seconds < EXPIRY_SAFETY_MARGIN_SECONDS:
            raise ControllerRelayTransportError(
                f"HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY: Token remaining lifetime "
                f"({remaining_seconds:.1f}s) is below required safety margin ({EXPIRY_SAFETY_MARGIN_SECONDS}s)"
            )

        return self._token
