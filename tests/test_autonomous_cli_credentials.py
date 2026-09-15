from aos import autonomous_host, secure_store

def test_goal_mode_cli_hydrates_secure_store_without_secret_values(monkeypatch):
    calls=[]
    monkeypatch.setattr(secure_store,'hydrate_environment', lambda overwrite=False: calls.append(overwrite) or {'GEMINI': True, 'GROQ': True})
    result=autonomous_host.hydrate_local_reasoning_credentials()
    assert result == {'GEMINI': True, 'GROQ': True}
    assert calls == [False]
    assert 'API_KEY' not in repr(result)
