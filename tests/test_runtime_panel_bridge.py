import aos.runtime_panel_bridge as bridge
import pytest


class FakeClient:
    def continue_project(self, **kwargs):
        return {
            "accepted": True,
            "command_id": "x",
            "state": "QUEUED",
            "workspace": "C:/workspace/lari-ui-v2",
            "descriptor_path": "C:/runtime/descriptors/lari-ui-v2.json",
            "routing_policy_path": "C:/runtime/descriptors/policy.json",
            **kwargs,
        }

    def health(self):
        return {"runtime_state": "HEALTHY", "active_commands": [], "production": "NO_GO", "ag_backend_enabled": False}


def test_panel_goal_uses_runtime_api_not_job_inbox(monkeypatch):
    monkeypatch.setattr(bridge, "_client", lambda config: FakeClient())
    config = {
        "runtime_api_url": "x",
        "runtime_token_path": "y",
        "projects": {"lari-ui-v2": {"project_id": "lari-ui-v2"}},
    }
    result = bridge.submit_goal_to_runtime(
        {"goal": "continue", "project_id": "lari-ui-v2"},
        config,
    )
    assert result["accepted"] is True
    assert result["project_id"] == "lari-ui-v2"
    assert result["workspace"] == "C:/workspace/lari-ui-v2"
    assert result["mode"] == "RUNTIME_V1_AUTONOMOUS_GOAL"
    assert result["run_plan_required"] is False


def test_panel_goal_requires_known_explicit_project(monkeypatch):
    monkeypatch.setattr(bridge, "_client", lambda config: FakeClient())
    config = {
        "runtime_api_url": "x",
        "runtime_token_path": "y",
        "projects": {"lari": {"project_id": "lari"}},
    }
    with pytest.raises(ValueError, match="project_id is required"):
        bridge.submit_goal_to_runtime({"goal": "continue"}, config)
    with pytest.raises(ValueError, match="Unknown project_id: lari-ui-v2"):
        bridge.submit_goal_to_runtime(
            {"goal": "continue", "project_id": "lari-ui-v2"},
            config,
        )


def test_runtime_status_without_runtime_config_preserves_unknown_host_state():
    result = bridge.runtime_status({})
    assert result["host_state"] == "UNKNOWN"
    assert result["runtime_v1"]["runtime_state"] == "NOT_CONFIGURED"
