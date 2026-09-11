"""GitHub App Installation Token Manager for Controller Relay Hosting.

Implementation Authority: LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Responsibilities:
- Mint short-lived GitHub App RS256 JWTs using private key material.
- Exchange JWT for a short-lived GitHub installation access token.
- Hold installation tokens strictly in-memory with safety margin >= 120s.
- Zero logging, zero persistence, zero return to MCP clients.
- Pure cryptographic implementation using `cryptography` with zero external JWT dependencies.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aos.controller_relay_identity import InjectedInstallationTokenCredentialProvider

TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 120.0
CANONICAL_GITHUB_API_BASE = "https://api.github.com"


def _b64url_encode(data: bytes) -> str:
    """Encode bytes using unpadded base64url."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    """Decode unpadded or padded base64url string to bytes."""
    rem = len(data) % 4
    if rem > 0:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data.encode("ascii"))


def create_github_app_jwt(
    app_id: str,
    private_key_pem: bytes,
    clock: Optional[Callable[[], float]] = None,
    expiry_seconds: int = 540,
) -> str:
    """Generate an RS256 JWT for GitHub App authentication.

    GitHub allows max 10-minute (600s) token lifetime. We default to 540s (9 minutes)
    with a 60s clock skew window.
    """
    if not app_id or not isinstance(app_id, str):
        raise ValueError("app_id must be a non-empty string")

    now = int(clock() if clock else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,  # 60s in the past for clock drift
        "exp": now + expiry_seconds,
        "iss": app_id.strip(),
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    try:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    except Exception as exc:
        raise ValueError(f"Failed to load GitHub App private key: {exc}") from None

    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("GitHub App private key must be an RSA private key")

    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def parse_private_key_input(raw_key_input: str) -> bytes:
    """Parse raw private key string (base64-encoded PEM or raw PEM)."""
    if not raw_key_input or not isinstance(raw_key_input, str):
        raise ValueError("Private key input must be a non-empty string")

    stripped = raw_key_input.strip()
    if "-----BEGIN" in stripped:
        return stripped.encode("utf-8")

    # Try base64 decoding
    try:
        decoded = base64.b64decode(stripped)
        if b"-----BEGIN" in decoded:
            return decoded
    except Exception:
        pass

    raise ValueError("Private key must be either valid PEM text or base64-encoded PEM text")


class GitHubAppInstallationTokenManager:
    """Manages short-lived in-memory GitHub App installation access tokens.

    Provides InjectedInstallationTokenCredentialProvider instances with caching
    and automatic renewal when lifetime drops below safety margin.
    """

    def __init__(
        self,
        app_id: str,
        private_key_pem: bytes,
        installation_id: str,
        github_api_base: str = CANONICAL_GITHUB_API_BASE,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        if not app_id or not isinstance(app_id, str):
            raise ValueError("app_id must be a non-empty string")
        if not installation_id or not isinstance(installation_id, str):
            raise ValueError("installation_id must be a non-empty string")

        self._app_id = app_id.strip()
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id.strip()
        self._github_api_base = github_api_base.rstrip("/")
        self._clock = clock if clock else (lambda: datetime.now(timezone.utc))

        self._current_token: Optional[str] = None
        self._token_expires_at: Optional[datetime] = None

    def __repr__(self) -> str:
        return f"GitHubAppInstallationTokenManager(app_id={self._app_id!r}, installation_id={self._installation_id!r})"

    def get_credential_provider(self) -> InjectedInstallationTokenCredentialProvider:
        """Return an active InjectedInstallationTokenCredentialProvider, refreshing if needed."""
        token, expires_at = self._get_or_refresh_token()
        return InjectedInstallationTokenCredentialProvider(
            token=token,
            expires_at=expires_at,
            clock=self._clock,
        )

    def _get_or_refresh_token(self) -> Tuple[str, datetime]:
        now = self._clock()
        if (
            self._current_token is not None
            and self._token_expires_at is not None
            and (self._token_expires_at - now).total_seconds() > TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS
        ):
            return self._current_token, self._token_expires_at

        # Need new installation token
        token, expires_at = self._request_installation_token()
        self._current_token = token
        self._token_expires_at = expires_at
        return token, expires_at

    def _request_installation_token(self) -> Tuple[str, datetime]:
        """Request short-lived installation access token from GitHub API."""
        now_ts = self._clock().timestamp()
        jwt_token = create_github_app_jwt(
            app_id=self._app_id,
            private_key_pem=self._private_key_pem,
            clock=lambda: now_ts,
        )

        url = f"{self._github_api_base}/app/installations/{self._installation_id}/access_tokens"
        req = urllib.request.Request(
            url,
            data=b"{}",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"Bearer {jwt_token}",
                "User-Agent": "AOS-Controller-Relay-Host",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8", errors="replace") if err.fp else ""
            raise RuntimeError(f"GitHub App installation token request failed (HTTP {err.code}): {err_body}") from None
        except Exception as exc:
            raise RuntimeError(f"GitHub App installation token request failed: {exc}") from None

        if status not in (200, 201):
            raise RuntimeError(f"Unexpected status from token endpoint: HTTP {status}")

        data = json.loads(body)
        token = data.get("token")
        expires_at_str = data.get("expires_at")

        if not token or not isinstance(token, str):
            raise RuntimeError("Missing or invalid 'token' in GitHub installation token response")
        if not expires_at_str or not isinstance(expires_at_str, str):
            raise RuntimeError("Missing or invalid 'expires_at' in GitHub installation token response")

        # Parse ISO8601 string (e.g. 2026-09-11T14:30:00Z)
        try:
            clean_str = expires_at_str.replace("Z", "+00:00")
            expires_at = datetime.fromisoformat(clean_str).astimezone(timezone.utc)
        except Exception as exc:
            raise RuntimeError(f"Failed to parse token expires_at timestamp '{expires_at_str}': {exc}") from None

        return token, expires_at
