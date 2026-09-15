from pathlib import Path

import pytest

from aos.runtime_contract import ContinueProjectCommand, ProjectProfile, validate_runtime_config


def _project(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ProjectProfile(
        project_id="lari",
        descriptor_path=str(descriptor),
        workspace=str(workspace),
        routing_policy_path=str(policy),
    )


def test_goal_only_command_requires_no_run_plan(tmp_path):
    cmd = ContinueProjectCommand.from_mapping({
        "goal": "Continue LARI Program V2 to completion within standing authority.",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }, project=_project(tmp_path))
    payload = cmd.to_dict()
    assert payload["command_type"] == "continue_project"
    assert "run_plan" not in payload
    assert payload["production"] == "NO_GO"
    assert payload["ag_backend_enabled"] is False


def test_secret_bearing_runtime_command_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="secret-bearing"):
        ContinueProjectCommand.from_mapping({
            "goal": "continue",
            "api_key": "should-never-enter-contract",
        }, project=_project(tmp_path))


def test_production_and_ag_are_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="production=NO_GO"):
        ContinueProjectCommand.from_mapping({"goal": "continue", "production": "GO"}, project=_project(tmp_path))
    with pytest.raises(ValueError, match="ag_backend_enabled=false"):
        ContinueProjectCommand.from_mapping({"goal": "continue", "ag_backend_enabled": True}, project=_project(tmp_path))


def test_runtime_config_binds_default_project(tmp_path):
    project = _project(tmp_path)
    cfg = validate_runtime_config({
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {"lari": project.to_dict()},
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })
    assert cfg["default_project"] == "lari"
    assert cfg["bind_host"] == "127.0.0.1"
