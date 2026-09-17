from pathlib import Path

import pytest

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
        assert spawned == [(result["command_id"], False)]
    finally:
        engine.shutdown()


def test_runtime_http_server_endpoints_and_security(tmp_path, monkeypatch):
    import json
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    import threading
    from aos.runtime_server import RuntimeHandler

    cfg = _config(tmp_path)
    engine = RuntimeEngine(cfg)
    token_val = "x" * 48
    (tmp_path / "token").write_text(token_val, encoding="utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    server.engine = engine  # type: ignore[attr-defined]
    server.runtime_token = token_val  # type: ignore[attr-defined]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. /v1/health is unauthenticated and returns HEALTHY
        with urllib.request.urlopen(f"{base_url}/v1/health", timeout=5) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["runtime_state"] == "HEALTHY"
            assert data["production"] == "NO_GO"
            assert data["ag_backend_enabled"] is False

        # 2. Unauthenticated GET /v1/commands/foo returns 403 Forbidden
        req = urllib.request.Request(f"{base_url}/v1/commands/foo")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 403

        # 3. OPTIONS request is rejected with METHOD_NOT_ALLOWED (CORS disabled)
        req_options = urllib.request.Request(f"{base_url}/v1/commands/continue", method="OPTIONS")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_options, timeout=5)
        assert exc_info.value.code == 405

        # 4. Authenticated GET for nonexistent command returns 404
        req_auth = urllib.request.Request(
            f"{base_url}/v1/commands/nonexistent-123",
            headers={"X-AOS-Runtime-Token": token_val},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_auth, timeout=5)
        assert exc_info.value.code == 404
    finally:
        server.shutdown()
        engine.shutdown()

