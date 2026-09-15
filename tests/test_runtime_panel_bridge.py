import aos.runtime_panel_bridge as bridge


class FakeClient:
    def continue_project(self, **kwargs):
        return {"accepted": True, "command_id": "x", "state": "QUEUED", **kwargs}

    def health(self):
        return {"runtime_state": "HEALTHY", "active_commands": [], "production": "NO_GO", "ag_backend_enabled": False}


def test_panel_goal_uses_runtime_api_not_job_inbox(monkeypatch):
    monkeypatch.setattr(bridge, "_client", lambda config: FakeClient())
    result = bridge.submit_goal_to_runtime({"goal": "continue"}, {"runtime_api_url": "x", "runtime_token_path": "y"})
    assert result["accepted"] is True
    assert result["mode"] == "RUNTIME_V1_AUTONOMOUS_GOAL"
    assert result["run_plan_required"] is False


def test_runtime_status_without_runtime_config_preserves_unknown_host_state():
    result = bridge.runtime_status({})
    assert result["host_state"] == "UNKNOWN"
    assert result["runtime_v1"]["runtime_state"] == "NOT_CONFIGURED"
