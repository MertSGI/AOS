import json
from pathlib import Path

import pytest

from aos.local_host import load_config, validate_job


def _config(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(tmp_path / "runtime"),
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }), encoding="utf-8")
    return load_config(cfg)


def _valid_job(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "schema_version": "1.0.0",
        "job_id": "job-1",
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


def test_validate_job_accepts_bounded_no_go_job(tmp_path):
    job = _valid_job(tmp_path)
    normalized = validate_job(job, _config(tmp_path))
    assert normalized["production"] == "NO_GO"
    assert normalized["ag_backend_enabled"] is False


def test_validate_job_rejects_path_escape(tmp_path):
    job = _valid_job(tmp_path)
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    job["workspace"] = str(outside)
    with pytest.raises(ValueError, match="outside authorized roots"):
        validate_job(job, _config(tmp_path))


def test_validate_job_rejects_production_go(tmp_path):
    job = _valid_job(tmp_path)
    job["production"] = "GO"
    with pytest.raises(ValueError, match="NO_GO"):
        validate_job(job, _config(tmp_path))


def test_validate_job_rejects_secret_keys(tmp_path):
    job = _valid_job(tmp_path)
    job["run_plan"]["api_key"] = "do-not-store"
    with pytest.raises(ValueError, match="secret"):
        validate_job(job, _config(tmp_path))


def test_download_import_does_not_replay_processed_job(tmp_path):
    from aos.local_host import _import_download_jobs

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    runtime = tmp_path / "runtime"
    inbox = runtime / "inbox"
    processed = runtime / "processed"
    failed = runtime / "failed"
    for p in (inbox, processed, failed):
        p.mkdir(parents=True, exist_ok=True)

    cfg_path = tmp_path / "config-download.json"
    cfg_path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(runtime),
        "download_watch_dir": str(downloads),
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }), encoding="utf-8")
    cfg = load_config(cfg_path)

    job = _valid_job(tmp_path)
    source = downloads / "job-1.aosjob.json"
    source.write_text(json.dumps(job), encoding="utf-8")
    assert _import_download_jobs(cfg, inbox, processed, failed) == 1
    (processed / source.name).write_text("{}", encoding="utf-8")
    (inbox / source.name).unlink()
    assert _import_download_jobs(cfg, inbox, processed, failed) == 0


def test_process_one_holds_incomplete_receipt(tmp_path, monkeypatch):
    import aos.local_host as local_host

    cfg = _config(tmp_path)
    job = _valid_job(tmp_path)
    job_path = tmp_path / "job-1.aosjob.json"
    job_path.write_text(json.dumps(job), encoding="utf-8")

    monkeypatch.setattr(local_host, "run_host", lambda **kwargs: {
        "progress": 50.0,
        "failed_task_ids": ["reasoning"],
        "ag_invocation_count": 0,
    })
    result = local_host.process_one(job_path, cfg, tmp_path / "runtime")
    assert result["status"] == "HOLD_INCOMPLETE_OR_FAILED"
