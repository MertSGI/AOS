import os

import pytest

import aos.secure_store as secure_store


def test_provider_name_validation():
    with pytest.raises(ValueError, match="Unsupported provider"):
        secure_store._normalize_provider("unknown")
    assert secure_store._normalize_provider("gemini") == "GEMINI"


def test_hydrate_environment_uses_store_without_returning_secret(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        secure_store,
        "read_provider_secret",
        lambda provider: "secret-value-for-test" if provider == "GEMINI" else None,
    )
    state = secure_store.hydrate_environment(overwrite=True)
    assert state["GEMINI"] is True
    assert os.environ["GEMINI_API_KEY"] == "secret-value-for-test"
    assert "secret-value-for-test" not in repr(state)


def test_provider_presence_combines_environment_and_store(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-secret")
    monkeypatch.setattr(secure_store, "read_provider_secret", lambda provider: None)
    state = secure_store.provider_presence()
    assert state["GROQ"] is True
    assert "env-secret" not in repr(state)
