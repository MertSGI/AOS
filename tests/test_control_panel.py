import json
from pathlib import Path

import pytest

import aos.control_panel as control_panel
from aos.control_panel import build_status, configure_provider, submit_job


def _config(tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return {
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(runtime),
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def _job(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "schema_version": "1.0.0",
        "job_id": "panel-job-1",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "run_plan": {
            "schema_version": "1.0.0",
            "project_id": "test",
            "bound_source_sha": "a" * 40,
            "tasks": [{"node_id": "x", "run_type": "TEST", "authority_id": "AUTH"}],
        },
    }


def test_submit_job_queues_valid_envelope(tmp_path):
    cfg = _config(tmp_path)
    result = submit_job(_job(tmp_path), cfg)
    assert result["accepted"] is True
    assert result["production"] == "NO_GO"
    assert result["ag_backend_enabled"] is False
    assert (Path(cfg["runtime_root"]) / "inbox" / "panel-job-1.aosjob.json").is_file()


def test_submit_job_rejects_duplicate(tmp_path):
    cfg = _config(tmp_path)
    job = _job(tmp_path)
    submit_job(job, cfg)
    with pytest.raises(ValueError, match="already exists"):
        submit_job(job, cfg)


def test_submit_job_rejects_secret_bearing_key(tmp_path):
    cfg = _config(tmp_path)
    job = _job(tmp_path)
    job["api_key"] = "never-store"
    with pytest.raises(ValueError, match="secret"):
        submit_job(job, cfg)


def test_build_status_is_fail_closed_and_ag_disabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    status = build_status(cfg)
    assert status["production"] == "NO_GO"
    assert status["ag_backend_enabled"] is False
    assert status["host_state"] == "UNKNOWN"


def test_configure_provider_never_returns_secret(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        control_panel,
        "write_provider_secret",
        lambda provider, secret: captured.update(provider=provider, secret=secret),
    )
    result = configure_provider({
        "provider": "GEMINI",
        "action": "save",
        "secret": "super-secret-value",
    })
    assert captured == {"provider": "GEMINI", "secret": "super-secret-value"}
    assert result["ready"] is True
    assert result["secret_returned"] is False
    assert "super-secret-value" not in repr(result)


def test_configure_provider_delete(monkeypatch):
    monkeypatch.setattr(control_panel, "delete_provider_secret", lambda provider: True)
    result = configure_provider({"provider": "GROQ", "action": "delete"})
    assert result["provider"] == "GROQ"
    assert result["deleted"] is True
    assert result["secret_returned"] is False
