import json
import subprocess

from aos.workers.codex_cli_probe import (
    build_codex_child_environment,
    parse_codex_doctor_auth,
    parse_codex_rate_limits,
    resolve_codex_capability_status,
    run_codex_cli_probe,
)


IDENTITY = {
    "path": "codex-test-double",
    "filename": "codex-test-double",
    "sha256": "d" * 64,
    "version": "codex-cli test",
}


def test_doctor_requires_chatgpt_tokens_and_rejects_api_key():
    valid = parse_codex_doctor_auth({
        "authentication": {
            "stored auth mode": "chatgpt",
            "stored ChatGPT tokens": "yes",
            "stored API key": "no",
        }
    })
    assert valid["chatgpt_subscription_usable"] is True
    api = parse_codex_doctor_auth({
        "auth_mode": "api_key", "stored_chatgpt_tokens": False, "stored_api_key": True,
    })
    assert api["chatgpt_subscription_usable"] is False
    assert api["api_key_present"] is True


def test_rate_limit_payload_maps_availability_scarcity_and_exhaustion():
    available = parse_codex_rate_limits({"result": {"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": 38, "resetsAt": 100},
        "secondary": {"usedPercent": 6, "resetsAt": 200},
        "rateLimitReachedType": None,
    }}}}, observed_at="2026-09-24T00:00:00+00:00")
    assert available["state"] == "AVAILABLE"
    scarce = parse_codex_rate_limits({"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": 95},
    }}})
    assert scarce["state"] == "LOW_OR_SCARCE"
    exhausted = parse_codex_rate_limits({"rateLimitsByLimitId": {"codex": {
        "primary": {"usedPercent": 100, "resetsAt": 300},
        "rateLimitReachedType": "primary",
    }}})
    assert exhausted["state"] == "QUOTA_EXHAUSTED"
    assert exhausted["retry_after_epoch"] == 300


def test_probe_persists_hash_bound_chatgpt_attestation_without_api_env(tmp_path):
    store = tmp_path / "codex-cli.json"
    seen_env = {}

    def doctor(argv, env):
        seen_env.update(env)
        report = {"authentication": {
            "auth_mode": "chatgpt",
            "stored_chatgpt_tokens": True,
            "stored_api_key": False,
        }}
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    attestation = run_codex_cli_probe(
        store_path=store,
        doctor_runner=doctor,
        quota_reader=lambda: {"state": "AVAILABLE", "source": "TEST"},
        identity_resolver=lambda command: IDENTITY,
        aos_revision="a" * 40,
    )
    assert attestation["capability_status"] == "PROVEN"
    assert attestation["extensions"]["codex_cli"]["paid_api_fallback"] == "DISABLED"
    assert resolve_codex_capability_status(identity=IDENTITY, store_path=store) == "PROVEN"
    assert "OPENAI_API_KEY" not in seen_env

    drifted = dict(IDENTITY, sha256="e" * 64)
    assert resolve_codex_capability_status(identity=drifted, store_path=store) == "UNPROVEN"


def test_child_environment_removes_all_api_credentials():
    env = build_codex_child_environment({
        "PATH": "safe",
        "OPENAI_API_KEY": "secret",
        "CODEX_API_KEY": "secret",
        "CODEX_ACCESS_TOKEN": "secret",
        "OPENAI_ACCESS_TOKEN": "secret",
        "AZURE_OPENAI_API_KEY": "secret",
        "OPENAI_ORG_ID": "secret",
        "OPENAI_PROJECT_ID": "secret",
    })
    assert env == {"PATH": "safe"}
