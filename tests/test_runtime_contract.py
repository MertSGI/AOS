import json
from pathlib import Path

import pytest

from aos.runtime_contract import (
    ContinueProjectCommand,
    ProjectProfile,
    cumulative_completed_batch_count,
    validate_configured_project_profiles,
    validate_runtime_config,
)


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


def _valid_project(tmp_path: Path, project_id: str = "lari") -> ProjectProfile:
    repo_root = Path(__file__).resolve().parents[1]
    descriptor = tmp_path / f"{project_id}.descriptor.json"
    descriptor_data = json.loads(
        (repo_root / "descriptors" / "lari.autonomous-host.descriptor.json").read_text(encoding="utf-8")
    )
    descriptor_data["project_id"] = project_id
    descriptor.write_text(json.dumps(descriptor_data), encoding="utf-8")
    policy = tmp_path / f"{project_id}.policy.json"
    policy.write_text(
        (repo_root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    workspace = tmp_path / f"{project_id}-workspace"
    workspace.mkdir(exist_ok=True)
    return ProjectProfile(project_id, str(descriptor), str(workspace), str(policy))


def _runtime_config(tmp_path: Path, projects):
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "authorized_roots": [str(tmp_path)],
        "projects": {profile.project_id: profile.to_dict() for profile in projects},
        "default_project": projects[0].project_id,
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def test_every_configured_project_profile_must_validate(tmp_path):
    valid = _valid_project(tmp_path, "lari")
    invalid = _valid_project(tmp_path, "lari-ui-v2")
    Path(invalid.descriptor_path).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid project descriptor"):
        validate_configured_project_profiles(_runtime_config(tmp_path, [valid, invalid]))


def test_descriptor_project_identity_must_match_profile(tmp_path):
    profile = _valid_project(tmp_path, "lari-ui-v2")
    descriptor_path = Path(profile.descriptor_path)
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["project_id"] = "lari"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match configured project_id"):
        validate_configured_project_profiles(_runtime_config(tmp_path, [profile]))


def test_configured_profile_missing_descriptor_or_policy_fails_closed(tmp_path):
    profile = _valid_project(tmp_path)
    Path(profile.descriptor_path).unlink()
    with pytest.raises(ValueError, match="File not found"):
        validate_configured_project_profiles(_runtime_config(tmp_path, [profile]))

    profile = _valid_project(tmp_path)
    Path(profile.routing_policy_path).unlink()
    with pytest.raises(ValueError, match="File not found"):
        validate_configured_project_profiles(_runtime_config(tmp_path, [profile]))


def test_cumulative_count_precedence_ignores_bounded_recent_window():
    assert cumulative_completed_batch_count({
        "total_completed_batch_count": 97,
        "completed_batch_count": 88,
        "completed_batches": [{}] * 30,
    }) == 97
    assert cumulative_completed_batch_count({
        "completed_batch_count": 88,
        "completed_batches": [{}] * 30,
    }) == 88
    assert cumulative_completed_batch_count({"completed_batches": [{}] * 30}) == 30
    assert cumulative_completed_batch_count({"phase": "PLANNING"}, {"completed_batch_count": 97}) == 97
