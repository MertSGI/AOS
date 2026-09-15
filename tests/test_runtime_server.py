from pathlib import Path

from aos.runtime_server import RuntimeEngine


def _config(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def test_runtime_engine_accepts_only_goal_for_default_project(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    spawned = []
    monkeypatch.setattr(engine, "_spawn_worker", lambda command_id, recovered: spawned.append((command_id, recovered)) or 999)
    try:
        result = engine.submit_continue({
            "goal": "Continue LARI Program V2 to completion within standing authority.",
            "production": "NO_GO",
            "ag_backend_enabled": False,
        })
        assert result["accepted"] is True
        assert result["run_plan_required"] is False
        assert result["project_id"] == "lari"
        command = engine.store.read_command(result["command_id"])
        assert command["project"]["project_id"] == "lari"
        assert "run_plan" not in command
        assert spawned == [(result["command_id"], False)]
    finally:
        engine.shutdown()
