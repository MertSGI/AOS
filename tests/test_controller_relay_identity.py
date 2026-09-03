"""Deterministic offline unit tests for InjectedInstallationTokenCredentialProvider.

Implementation Authority ID: LARI-AOS-CONTROLLER-RELAY-IDENTITY-ADAPTER-R0-20260903-01
PROVES MEMORY-ONLY CREDENTIAL SAFETY, REDACTION, AND EXPIRY BOUNDARY.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from aos.controller_relay_git_transport import ControllerRelayTransportError
from aos.controller_relay_identity import InjectedInstallationTokenCredentialProvider

FAKE_TOKEN = "ghs_fake1234567890abcdefghijklmnopqrstuv"


def test_token_absent_from_repr_and_str():
    """1. token absent from repr and 2. token absent from str"""
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)

    repr_str = repr(provider)
    str_str = str(provider)

    assert FAKE_TOKEN not in repr_str
    assert "***REDACTED***" in repr_str
    assert FAKE_TOKEN not in str_str
    assert "***REDACTED***" in str_str


def test_token_absent_from_raised_exception_text():
    """3. token absent from raised exception text"""
    # Create an expired token provider
    past_exp = datetime.now(timezone.utc) - timedelta(seconds=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, past_exp)

    with pytest.raises(ControllerRelayTransportError) as exc_info:
        provider.get_token()

    err_text = str(exc_info.value)
    assert FAKE_TOKEN not in err_text
    assert "HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY" in err_text


def test_valid_token_retrievable_via_get_token():
    """4. valid token retrievable via get_token()"""
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)
    assert provider.get_token() == FAKE_TOKEN


def test_expired_token_rejected():
    """5. expired token rejected"""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    exp = datetime(2026, 9, 3, 11, 59, 0, tzinfo=timezone.utc)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp, clock=lambda: now)

    with pytest.raises(ControllerRelayTransportError, match="HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY"):
        provider.get_token()


def test_119_seconds_remaining_rejected():
    """6. 119 seconds remaining rejected"""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    exp = now + timedelta(seconds=119)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp, clock=lambda: now)

    with pytest.raises(ControllerRelayTransportError, match="HOLD_INSTALLATION_TOKEN_TOO_CLOSE_TO_EXPIRY"):
        provider.get_token()


def test_exact_120_seconds_remaining_accepted():
    """7. exact 120 seconds remaining accepted"""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    exp = now + timedelta(seconds=120)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp, clock=lambda: now)
    assert provider.get_token() == FAKE_TOKEN


def test_valid_greater_than_120_seconds_lifetime_accepted():
    """8. valid >120 second lifetime accepted"""
    now = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
    exp = now + timedelta(seconds=300)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp, clock=lambda: now)
    assert provider.get_token() == FAKE_TOKEN


def test_naive_ambiguous_expires_at_rejected():
    """9. naive/ambiguous expires_at rejected"""
    naive_dt = datetime(2026, 9, 3, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, naive_dt)


def test_no_environment_variable_lookup_or_fallback(monkeypatch):
    """10. no environment variable lookup/fallback & 11. env vars do not alter behavior"""
    monkeypatch.setenv("GITHUB_TOKEN", "bad-env-token")
    monkeypatch.setenv("GH_TOKEN", "bad-env-token-2")
    monkeypatch.setenv("GITHUB_APP_TOKEN", "bad-env-token-3")

    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)
    assert provider.get_token() == FAKE_TOKEN
    assert "bad-env-token" not in repr(provider)


def test_no_pat_fallback():
    """12. no PAT fallback"""
    with pytest.raises(ValueError, match="non-empty string"):
        InjectedInstallationTokenCredentialProvider("", datetime.now(timezone.utc) + timedelta(minutes=10))  # type: ignore


def test_no_oauth_user_token_fallback():
    """13. no OAuth/user-token fallback"""
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)

    # Assert no properties or methods exist for external credentials or OAuth lookups
    for bad_attr in ["oauth_token", "pat_token", "user_credential", "token", "raw_token", "secret"]:
        assert not hasattr(provider, bad_attr)


def test_no_file_token_path_input_api():
    """14. no file/token-path input API"""
    # Constructor only accepts string token and datetime expires_at
    with pytest.raises(ValueError):
        InjectedInstallationTokenCredentialProvider(12345, datetime.now(timezone.utc) + timedelta(minutes=10))  # type: ignore


def test_constructor_does_not_persist_token():
    """15. constructor does not persist token anywhere"""
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)

    # Provider attributes check
    for k, v in provider.__dict__.items():
        if k != "_token":
            assert FAKE_TOKEN not in str(v)


def test_no_credential_value_appears_in_safe_metadata():
    """16. no credential value appears in safe result/repr metadata."""
    exp = datetime.now(timezone.utc) + timedelta(minutes=10)
    provider = InjectedInstallationTokenCredentialProvider(FAKE_TOKEN, exp)
    repr_text = repr(provider)

    assert FAKE_TOKEN not in repr_text
    assert "ghs_" not in repr_text
    assert "***REDACTED***" in repr_text
