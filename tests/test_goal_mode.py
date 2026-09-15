import json
from pathlib import Path

from aos.autonomous_host import build_parser
from aos.control_panel import submit_goal
from aos.local_host import validate_job


def _config(tmp_path):
    descriptor = tmp_path / "descriptor.json"
    workspace = tmp_path / "workspace"
    policy = tmp_path / "policy.json"
    descriptor.write_text("{}", encoding="utf-8")
    workspace.mkdir()
    policy.write_text("{}", encoding="utf-8")
    return {
        "schema_version": "1.0.0",
        "authorized_roots": [str(tmp_path)],
        "runtime_root": str(tmp_path / "runtime"),
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }, descriptor, workspace, policy


def test_cli_run_plan_is_optional_for_normal_mode():
    args = build_parser().parse_args(["--project", "descriptor.json"])
    assert args.run_plan is None
    assert args.workspace == "."
    assert args.goal


def test_local_host_accepts_goal_without_run_plan(tmp_path):
    cfg, descriptor, workspace, policy = _config(tmp_path)
    job = {
        "schema_version": "1.0.0",
        "job_id": "goal-proof",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "goal": "Continue project to completion under standing authority.",
        "constraints": [],
        "red_lines": [],
    }
    normalized = validate_job(job, cfg)
    assert normalized["goal"].startswith("Continue")
    assert "run_plan" not in normalized


def test_local_host_rejects_goal_and_run_plan_together(tmp_path):
    cfg, descriptor, workspace, policy = _config(tmp_path)
    job = {
        "schema_version": "1.0.0",
        "job_id": "bad",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "goal": "Continue",
        "run_plan": {"schema_version": "1.0.0", "tasks": [{"node_id": "x"}]},
    }
    import pytest
    with pytest.raises(ValueError, match="exactly one"):
        validate_job(job, cfg)


def test_control_panel_goal_submission_constructs_nonproduction_goal_job(tmp_path, monkeypatch):
    cfg, descriptor, workspace, policy = _config(tmp_path)
    captured = {}

    def fake_submit(job, config):
        captured.update(job)
        return {"accepted": True, "job_id": job["job_id"]}

    monkeypatch.setattr("aos.control_panel.submit_job", fake_submit)
    result = submit_goal({
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "goal": "Continue autonomously",
        "constraints": ["non-production"],
        "red_lines": [],
        "max_batches": 4,
    }, cfg)
    assert result["run_plan_required"] is False
    assert captured["production"] == "NO_GO"
    assert captured["ag_backend_enabled"] is False
    assert captured["goal"] == "Continue autonomously"
    assert "run_plan" not in captured
