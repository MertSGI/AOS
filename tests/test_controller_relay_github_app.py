"""Tests for GitHubAppInstallationTokenManager and RS256 JWT minting.

Implementation Authority: LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aos.controller_relay_github_app import (
    GitHubAppInstallationTokenManager,
    create_github_app_jwt,
    parse_private_key_input,
)


@pytest.fixture(scope="module")
def sample_rsa_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_create_github_app_jwt(sample_rsa_key_pem):
    fixed_now = 1757590000.0
    jwt_str = create_github_app_jwt("12345", sample_rsa_key_pem, clock=lambda: fixed_now)
    parts = jwt_str.split(".")
    assert len(parts) == 3

    # Decode header
    header_raw = base64.urlsafe_b64decode(parts[0] + "==")
    header = json.loads(header_raw)
    assert header == {"alg": "RS256", "typ": "JWT"}

    # Decode payload
    payload_raw = base64.urlsafe_b64decode(parts[1] + "==")
    payload = json.loads(payload_raw)
    assert payload["iss"] == "12345"
    assert payload["iat"] == int(fixed_now) - 60
    assert payload["exp"] == int(fixed_now) + 540


def test_parse_private_key_input(sample_rsa_key_pem):
    # Raw PEM
    parsed1 = parse_private_key_input(sample_rsa_key_pem.decode("utf-8"))
    assert b"-----BEGIN" in parsed1

    # Base64-encoded PEM
    b64_str = base64.b64encode(sample_rsa_key_pem).decode("ascii")
    parsed2 = parse_private_key_input(b64_str)
    assert parsed2 == sample_rsa_key_pem

    # Invalid input
    with pytest.raises(ValueError):
        parse_private_key_input("not-a-valid-pem-or-b64")


def test_installation_token_manager_in_memory_refresh(sample_rsa_key_pem, monkeypatch):
    """Test caching and automatic renewal of in-memory tokens."""
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
    clock_val = [now]

    manager = GitHubAppInstallationTokenManager(
        app_id="app_123",
        private_key_pem=sample_rsa_key_pem,
        installation_id="inst_456",
        clock=lambda: clock_val[0],
    )

    call_count = 0

    def mock_request(self):
        nonlocal call_count
        call_count += 1
        expires = clock_val[0] + timedelta(minutes=10)
        return f"token_v{call_count}", expires

    monkeypatch.setattr(manager, "_request_installation_token", lambda: mock_request(manager))

    # First call fetches token_v1
    cred1 = manager.get_credential_provider()
    assert cred1.get_token() == "token_v1"
    assert call_count == 1

    # Second call within safety window reuses token_v1 without calling API
    clock_val[0] += timedelta(minutes=5)
    cred2 = manager.get_credential_provider()
    assert cred2.get_token() == "token_v1"
    assert call_count == 1

    # Advance clock to within 100s of expiry (< 120s safety margin) -> refreshes to token_v2
    clock_val[0] += timedelta(minutes=4, seconds=10)
    cred3 = manager.get_credential_provider()
    assert cred3.get_token() == "token_v2"
    assert call_count == 2
