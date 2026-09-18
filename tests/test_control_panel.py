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


def test_build_status_includes_deliberation_and_lane_telemetry(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    # Unconfigured case contains zeroed deliberation structure
    status = build_status(cfg)
    assert "deliberation" in status
    assert status["deliberation"]["total_samples"] == 0
    assert status["deliberation"]["trigger_reasons"] == {}

    # Configured runtime case with deliberation ledger in store
    state_dir = tmp_path / "state"
    commands_dir = state_dir / "commands"
    cmd_dir = commands_dir / "cmd-123"
    delib_dir = cmd_dir / "project-runtime" / "deliberation"
    delib_dir.mkdir(parents=True)
    ledger_file = delib_dir / "deliberation-shadow-ledger.jsonl"
    entry = {
        "decision_id": "dec-1",
        "council_trigger_reason": "MATERIAL_AMBIGUITY_OR_HIGH_IMPACT",
        "quorum_obtained": True,
        "council_agreement": True,
    }
    ledger_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    # Mock runtime_configured and runtime_status
    monkeypatch.setattr(control_panel, "runtime_configured", lambda c: True)
    monkeypatch.setattr(
        control_panel,
        "runtime_status",
        lambda c: {
            "host_state": "RUNNING",
            "runtime_v1": {
                "runtime_state": "HEALTHY",
                "active_commands": ["cmd-123"],
                "waiting_commands": [],
            },
        },
    )
    # Set LOCALAPPDATA to point to tmp_path so it scans state_dir
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path.parent))
    fake_aos_state = tmp_path.parent / "AOS" / "runtime-v1" / "state"
    fake_aos_state.mkdir(parents=True, exist_ok=True)
    (fake_aos_state / "commands").mkdir(exist_ok=True)

    # Point cfg runtime_root to tmp_path
    cfg["runtime_root"] = str(tmp_path)
    status2 = build_status(cfg)
    assert status2["deliberation"]["total_samples"] == 1
    assert status2["deliberation"]["quorum_count"] == 1
    assert status2["deliberation"]["agreement_count"] == 1
    assert status2["deliberation"]["trigger_reasons"]["MATERIAL_AMBIGUITY_OR_HIGH_IMPACT"] == 1

