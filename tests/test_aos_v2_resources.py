"""Tests for AOS V2 ResourceGateway: eligibility, budgets, zero-cost enforcement."""
import datetime
import json
import os
import pytest

from aos.v2.lifecycle import LifecycleStore
from aos.v2.registry import RegistrySnapshot
from aos.v2.resources import (
    GenerationObservation,
    ResourceBudgetExceededError,
    ResourceContentError,
    ResourceGateway,
    ResourceProfile,
    ResourceUnavailableError,
    check_eligibility,
)


def _fresh_iso(delta_seconds: float = 0) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=delta_seconds)
    return dt.isoformat()


@pytest.fixture
def valid_profile_dict():
    return {
        "id": "res-test-free",
        "enabled": True,
        "provider": "openai_compatible",
        "model": "free-gpt-test",
        "protocol": "chat.completions",
        "base_url": "https://api.free-provider.local/v1",
        "credential_ref": "FREE_PROVIDER_API_KEY",
        "capabilities": {"json_output": True, "code": True},
        "context_window": 8192,
        "token_count_method": "approximate_chars",
        "entitlement": {
            "tier": "FREE",
            "hard_no_overage": True,
            "evidence_ref": "ref-zero-cost-proof-01",
            "observed_at": _fresh_iso(60),
            "expires_at": _fresh_iso(-3600),
        },
        "quota": {
            "remaining_requests": 50,
            "observed_at": _fresh_iso(30),
        },
        "health": {
            "status": "HEALTHY",
            "observed_at": _fresh_iso(30),
        },
        "allowed_data_classes": ["PUBLIC_DISPOSABLE_CODE"],
        "circuit": {"open": False, "failure_count": 0},
    }


def test_valid_profile_eligibility(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is True
    assert reason == "ELIGIBLE"


def test_reject_paid_or_unknown_cost(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    for bad_tier in ["PAID", "UNKNOWN", "PRO"]:
        valid_profile_dict["entitlement"]["tier"] = bad_tier
        p = ResourceProfile.from_dict(valid_profile_dict)
        ok, reason = check_eligibility(p)
        assert ok is False
        assert "ZERO_PAID" in reason


def test_reject_missing_hard_no_overage(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    valid_profile_dict["entitlement"]["hard_no_overage"] = False
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is False
    assert "hard_no_overage" in reason


def test_reject_stale_entitlement(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    # Observed 25 hours ago
    valid_profile_dict["entitlement"]["observed_at"] = _fresh_iso(25 * 3600)
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is False
    assert "24h" in reason


def test_reject_stale_quota_and_health(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    # Quota observed 6 minutes ago (> 5m limit)
    valid_profile_dict["quota"]["observed_at"] = _fresh_iso(360)
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is False
    assert "5 minutes" in reason

    # Restore quota, make health stale
    valid_profile_dict["quota"]["observed_at"] = _fresh_iso(30)
    valid_profile_dict["health"]["observed_at"] = _fresh_iso(400)
    p2 = ResourceProfile.from_dict(valid_profile_dict)
    ok2, reason2 = check_eligibility(p2)
    assert ok2 is False
    assert "Health" in reason2


def test_reject_missing_credential(valid_profile_dict, monkeypatch):
    monkeypatch.delenv("FREE_PROVIDER_API_KEY", raising=False)
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is False
    assert "not present in environment" in reason


def test_reject_circuit_open(valid_profile_dict, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    valid_profile_dict["circuit"]["open"] = True
    p = ResourceProfile.from_dict(valid_profile_dict)
    ok, reason = check_eligibility(p)
    assert ok is False
    assert "Circuit" in reason


def test_gateway_generation_with_mock_records_attempt_and_simulated_class(valid_profile_dict, tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x")
    from aos.v2.lifecycle import TrustedGrant
    grant = TrustedGrant("p1", "auth1", str(ws), "a" * 40, ("a.py",))
    store = LifecycleStore(tmp_path / "v2-runtime")
    store.admit("cmd-res-1", "req-res-1", "goal", ("crit-1",), grant)

    snapshot = RegistrySnapshot(
        path=str(tmp_path / "reg.json"),
        raw_hash="0" * 64,
        schema_version=1,
        projects={},
        resources={"res-test-free": valid_profile_dict},
    )

    def mock_gen(purpose, prompt, budget):
        return json.dumps({"plan": "ok"})

    gateway = ResourceGateway(snapshot, store, mock_generator=mock_gen)
    budget = {
        "max_network_attempts": 4,
        "per_request_timeout": 30.0,
        "max_context_bytes": 32768,
        "max_output_bytes": 8192,
    }

    obs = gateway.generate(
        command_id="cmd-res-1",
        purpose="PLAN",
        system_prompt="system",
        user_prompt="user prompt",
        budget=budget,
    )

    assert isinstance(obs, GenerationObservation)
    assert obs.evidence_class == "SIMULATED_MODEL"
    assert obs.billed_cost == "UNKNOWN"
    assert obs.content == json.dumps({"plan": "ok"})

    # Check attempt table in store
    with store._transaction() as db:
        attempts = db.execute("SELECT * FROM attempts WHERE command_id='cmd-res-1'").fetchall()
        assert len(attempts) == 1
        att = attempts[0]
        assert att["purpose"] == "PLAN"
        assert att["billed_cost"] == "UNKNOWN"


def test_gateway_enforces_attempt_budget(valid_profile_dict, tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("x")
    from aos.v2.lifecycle import TrustedGrant
    grant = TrustedGrant("p1", "auth1", str(ws), "a" * 40, ("a.py",))
    store = LifecycleStore(tmp_path / "v2-runtime")
    store.admit("cmd-budget", "req-budget", "goal", ("crit-1",), grant)

    snapshot = RegistrySnapshot(
        path=str(tmp_path / "reg.json"),
        raw_hash="0" * 64,
        schema_version=1,
        projects={},
        resources={"res-test-free": valid_profile_dict},
    )

    def mock_gen(purpose, prompt, budget):
        return "{}"

    gateway = ResourceGateway(snapshot, store, mock_generator=mock_gen)
    budget = {
        "max_network_attempts": 2,  # budget of 2
        "per_request_timeout": 30.0,
        "max_context_bytes": 32768,
        "max_output_bytes": 8192,
    }

    # First attempt succeeds
    gateway.generate("cmd-budget", "PLAN", "sys", "prompt", budget)
    # Second attempt succeeds
    gateway.generate("cmd-budget", "EDIT", "sys", "prompt", budget)
    # Third attempt exceeds budget -> raises ResourceBudgetExceededError
    with pytest.raises(ResourceBudgetExceededError):
        gateway.generate("cmd-budget", "EDIT", "sys", "prompt", budget)


def test_gateway_rejects_oversized_context(valid_profile_dict, tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_PROVIDER_API_KEY", "secret-test-token")
    store = LifecycleStore(tmp_path / "v2-runtime")
    snapshot = RegistrySnapshot(
        path=str(tmp_path / "reg.json"),
        raw_hash="0" * 64,
        schema_version=1,
        projects={},
        resources={"res-test-free": valid_profile_dict},
    )
    gateway = ResourceGateway(snapshot, store, mock_generator=lambda *args: "{}")
    budget = {
        "max_network_attempts": 4,
        "per_request_timeout": 30.0,
        "max_context_bytes": 100,  # small context bound
        "max_output_bytes": 8192,
    }
    with pytest.raises(ResourceContentError, match="Context size"):
        gateway.generate("cmd-ctx", "PLAN", "sys", "x" * 200, budget)
