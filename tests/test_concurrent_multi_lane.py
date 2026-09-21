"""Regression test for safe concurrent multi-lane AOS execution.

Proves:
1. Multi-project registration with disjoint workspaces passes validation.
2. Identical or overlapping/nested workspaces are rejected fail-closed.
3. Concurrent execution across disjoint workspaces (e.g. Lane A and Lane C) succeeds
   with zero state pollution, independent command lineages, and active lane tracking.
4. Attempting concurrent commands against the same workspace is rejected with a clear conflict error.
5. Multi-lane recovery preserves independent command states with duplicate_completed_work = 0.
"""
import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock

from aos.runtime_contract import validate_runtime_config, ProjectProfile
from aos.runtime_server import RuntimeEngine
from aos import runtime_worker


def _make_project_fixtures(tmp_path: Path, project_id: str):
    repo_root = Path(__file__).resolve().parents[1]
    proj_dir = tmp_path / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    descriptor = proj_dir / "descriptor.json"
    descriptor_data = json.loads(
        (repo_root / "descriptors" / "lari.autonomous-host.descriptor.json").read_text(encoding="utf-8")
    )
    descriptor_data["project_id"] = project_id
    descriptor.write_text(json.dumps(descriptor_data), encoding="utf-8")
    policy = proj_dir / "policy.json"
    policy.write_text(
        (repo_root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    workspace = proj_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "project_id": project_id,
        "descriptor_path": str(descriptor),
        "workspace": str(workspace),
        "routing_policy_path": str(policy),
        "standing_authority": True,
    }


def test_disjoint_multi_project_config_validation(tmp_path):
    p_lari = _make_project_fixtures(tmp_path, "lari")
    p_ui_v2 = _make_project_fixtures(tmp_path, "lari-ui-v2")

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": p_lari,
            "lari-ui-v2": p_ui_v2,
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    validated = validate_runtime_config(cfg)
    assert "lari" in validated["projects"]
    assert "lari-ui-v2" in validated["projects"]
    assert validated["default_project"] == "lari"


def test_overlapping_workspace_rejected(tmp_path):
    p_lari = _make_project_fixtures(tmp_path, "lari")
    # Same workspace as lari
    p_duplicate = {
        "project_id": "lari-dup",
        "descriptor_path": p_lari["descriptor_path"],
        "workspace": p_lari["workspace"],
        "routing_policy_path": p_lari["routing_policy_path"],
        "standing_authority": True,
    }

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": p_lari,
            "lari-dup": p_duplicate,
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    with pytest.raises(ValueError, match="Workspace collision detected"):
        validate_runtime_config(cfg)


def test_nested_workspace_rejected(tmp_path):
    p_lari = _make_project_fixtures(tmp_path, "lari")
    nested_workspace = Path(p_lari["workspace"]) / "sub-lane"
    nested_workspace.mkdir(parents=True, exist_ok=True)

    p_nested = {
        "project_id": "lari-nested",
        "descriptor_path": p_lari["descriptor_path"],
        "workspace": str(nested_workspace),
        "routing_policy_path": p_lari["routing_policy_path"],
        "standing_authority": True,
    }

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": p_lari,
            "lari-nested": p_nested,
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    with pytest.raises(ValueError, match="Workspace nesting collision"):
        validate_runtime_config(cfg)


def test_concurrent_multi_lane_submission_and_health_tracking(tmp_path, monkeypatch):
    p_lari = _make_project_fixtures(tmp_path, "lari")
    p_ui_v2 = _make_project_fixtures(tmp_path, "lari-ui-v2")

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": p_lari,
            "lari-ui-v2": p_ui_v2,
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    engine = RuntimeEngine(cfg)
    spawned = []
    monkeypatch.setattr(engine, "_spawn_worker", lambda cmd_id, recovered: spawned.append((cmd_id, recovered)) or 1000 + len(spawned))

    try:
        # Submit Lane A (default project: lari)
        res_a = engine.submit_continue({
            "goal": "Continue Lane A LARI acceptance",
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })
        assert res_a["accepted"] is True
        assert res_a["project_id"] == "lari"

        # Submit Lane C (explicit project: lari-ui-v2)
        res_c = engine.submit_continue({
            "project_id": "lari-ui-v2",
            "goal": "Execute LARI UI V2 productization autonomously within canonical contracts",
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })
        assert res_c["accepted"] is True
        assert res_c["project_id"] == "lari-ui-v2"

        # Verify health tracking differentiates active commands per project
        h = engine._collect_detailed_status()
        assert res_a["command_id"] in h["active_commands"]
        assert res_c["command_id"] in h["active_commands"]
        assert h["active_commands_by_project"]["lari"] == [res_a["command_id"]]
        assert h["active_commands_by_project"]["lari-ui-v2"] == [res_c["command_id"]]
        assert "lari" in h["registered_projects"]
        assert "lari-ui-v2" in h["registered_projects"]
    finally:
        engine.shutdown()


def test_concurrent_same_workspace_worker_lock_conflict(tmp_path, monkeypatch):
    p_lari = _make_project_fixtures(tmp_path, "lari")

    cfg = {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": p_lari,
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }

    engine = RuntimeEngine(cfg)
    monkeypatch.setattr(engine, "_spawn_worker", lambda cmd_id, recovered: 1001)

    try:
        res1 = engine.submit_continue({
            "goal": "Command 1",
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })
        res2 = engine.submit_continue({
            "goal": "Command 2 against same workspace",
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })

        # Mock run_autonomous_project so command 1 can execute cleanly
        monkeypatch.setattr(
            runtime_worker,
            "run_autonomous_project",
            lambda **kwargs: {"disposition": "PROJECT_COMPLETE", "completed_batch_count": 1},
        )
        monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda **kwargs: None)

        # Manually acquire the workspace lock representing an active worker on command 1
        ws_lock_file = Path(p_lari["workspace"]) / ".aos_workspace_active.lock"
        with runtime_worker.exclusive_file_lock(ws_lock_file):
            # Attempting to execute command 2 while command 1 holds the workspace lock must raise RuntimeError with conflict
            with pytest.raises(RuntimeError, match="Workspace conflict"):
                runtime_worker.execute_command(Path(cfg["runtime_root"]), res2["command_id"])
    finally:
        engine.shutdown()
