import os
import sys
from pathlib import Path

import pytest

from aos.runtime_server import RuntimeEngine


def _config(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        (repo_root / "descriptors" / "lari.autonomous-host.descriptor.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        (repo_root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
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
        assert result["workspace"] == str((tmp_path / "workspace").resolve())
        assert result["descriptor_path"] == str((tmp_path / "descriptor.json").resolve())
        assert result["routing_policy_path"] == str((tmp_path / "policy.json").resolve())
        command = engine.store.read_command(result["command_id"])
        assert spawned == [(result["command_id"], False)]
    finally:
        engine.shutdown()


def test_status_uses_most_recent_command_activity_not_lexical_id(tmp_path):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        engine.pause_safe()
        for command_id in ("continue-z-older", "continue-a-newer"):
            engine.store.create_command({
                "command_id": command_id,
                "project": {"project_id": "lari"},
                "goal": "status ordering proof",
            })
        engine.store.write_state(
            "continue-z-older",
            state="FAILED",
            disposition="FAILED",
        )
        engine.store.write_state(
            "continue-a-newer",
            state="HUMAN_REQUIRED",
            disposition="HUMAN_REQUIRED",
            failure_class="RECOVERY_CHURN_GUARD",
        )

        status = engine._collect_detailed_status()

        assert status["latest_command"]["command_id"] == "continue-a-newer"
        assert status["latest_command"]["failure_class"] == "RECOVERY_CHURN_GUARD"
        assert len(status["provider_circuit_registry_paths"]) == 1
        assert "continue-a-newer" in status["provider_circuit_registry_paths"][0]
    finally:
        engine.shutdown()


def test_invalid_project_profile_fails_before_worker_execution(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    Path(cfg["projects"]["lari"]["descriptor_path"]).write_text("{}", encoding="utf-8")
    engine = RuntimeEngine(cfg)
    spawned = []
    monkeypatch.setattr(engine, "_spawn_worker", lambda *args, **kwargs: spawned.append(args))
    try:
        with pytest.raises(ValueError, match="Invalid project descriptor"):
            engine.submit_continue({"goal": "must not execute"})
        assert spawned == []
        assert engine.store.list_command_ids() == []
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


def test_spawn_worker_environment_and_executable_resolution(tmp_path, monkeypatch):
    import subprocess
    from aos.runtime_server import (
        _build_worker_env,
        _resolve_worker_executable,
        load_config,
    )

    cfg = _config(tmp_path)
    slot_site = tmp_path / "candidate" / "site"
    slot_site.mkdir(parents=True)
    cfg["runtime_slot_root"] = str(tmp_path / "candidate")

    # Verify _build_worker_env includes candidate site and module parent
    worker_env = _build_worker_env(str(tmp_path / "candidate"))
    pythonpath = worker_env.get("PYTHONPATH", "")
    assert str(slot_site.resolve()) in pythonpath

    # Long-lived Windows workers use pythonw.exe when the sibling
    # executable exists. Other platforms keep the current interpreter.
    exe = _resolve_worker_executable()

    if (
        os.name == "nt"
        and
        Path(sys.executable)
        .with_name("pythonw.exe")
        .is_file()
    ):
        assert (
            Path(exe).name.lower()
            ==
            "pythonw.exe"
        )
    else:
        assert (
            Path(exe).resolve()
            ==
            Path(sys.executable).resolve()
        )

    # Test _spawn_worker passes env and resolved executable
    engine = RuntimeEngine(cfg)
    command_id = "test-cmd-spawn-env"
    engine.store.create_command({
        "command_id": command_id,
        "project": {"project_id": "lari"},
        "goal": "Test worker spawn environment",
        "contract_version": "1.0.0",
        "created_at": "2026-09-17T00:00:00Z",
    })

    captured = {}

    class DummyProc:
        pid = 77777

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProc()

    monkeypatch.setattr("aos.runtime_server.popen_headless", fake_popen)
    try:
        pid = engine._spawn_worker(command_id, recovered=False)
        assert pid == 77777
        assert captured["cmd"][0] == _resolve_worker_executable()
        assert captured["kwargs"]["env"]["PYTHONPATH"] == worker_env["PYTHONPATH"]
        assert captured["kwargs"]["close_fds"] is True
        assert captured["kwargs"]["detached"] is True
    finally:
        engine.shutdown()
