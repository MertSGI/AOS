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


def test_runtime_provider_ids_map_to_normalized_secure_store_identity(monkeypatch):
    expected = {
        "nemotron": ("NVIDIA_API_KEY", "NVIDIA"),
        "gemini": ("GEMINI_API_KEY", "GEMINI"),
        "groq": ("GROQ_API_KEY", "GROQ"),
        "cloudflare": ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE"),
        "openrouter_free": ("OPENROUTER_API_KEY", "OPENROUTER"),
        "cerebras": ("CEREBRAS_API_KEY", "CEREBRAS"),
        "huggingface_router": ("HF_TOKEN", "HUGGINGFACE"),
        "openai": ("OPENAI_API_KEY", "OPENAI"),
        "openai_paid_safety": ("OPENAI_API_KEY", "OPENAI"),
    }
    for provider_id, (env_var, identity) in expected.items():
        monkeypatch.delenv(env_var, raising=False)
        assert secure_store.resolve_credential_env_var(provider_id) == env_var
        assert secure_store.secure_store_identity(provider_id) == identity
        assert secure_store.credential_is_configured(
            provider_id,
            presence={identity: True},
        ) is True
