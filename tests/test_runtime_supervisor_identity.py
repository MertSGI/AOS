import json
import os
from pathlib import Path

import pytest

import aos.runtime_supervisor as runtime_supervisor_module
from aos.runtime_slots import SlotRecord
from aos.runtime_supervisor import RuntimeSupervisor, _runtime_health_matches


def _slot():
    return SlotRecord(
        "candidate-runtime-v1.6-abc", "runtime_v1", ("python", "runtime.py"),
        "a" * 40, "http://127.0.0.1:8770/v1/health", "runtime-config.json", "now",
    )


def test_runtime_supervisor_accepts_windows_launcher_pid_handoff():
    slot = _slot()
    good = {
        "pid": 18784,
        "runtime_supervisor_pid": 12144,
        "runtime_source_sha": "a" * 40,
        "runtime_slot_id": slot.slot_id,
        "runtime_launch_nonce": "nonce-1",
    }
    # API PID may differ from the short-lived venv launcher PID. Ownership is
    # bound to the actual supervisor PID, nonce, exact SHA and exact slot.
    assert _runtime_health_matches(slot, good, "nonce-1", 12144) is True


def test_runtime_supervisor_rejects_foreign_supervisor_nonce_sha_or_slot():
    slot = _slot()
    good = {
        "pid": 18784,
        "runtime_supervisor_pid": 12144,
        "runtime_source_sha": "a" * 40,
        "runtime_slot_id": slot.slot_id,
        "runtime_launch_nonce": "nonce-1",
    }
    assert _runtime_health_matches(slot, {**good, "runtime_supervisor_pid": 9999}, "nonce-1", 12144) is False
    assert _runtime_health_matches(slot, {**good, "runtime_launch_nonce": "other"}, "nonce-1", 12144) is False
    assert _runtime_health_matches(slot, {**good, "runtime_source_sha": "b" * 40}, "nonce-1", 12144) is False
    assert _runtime_health_matches(slot, {**good, "runtime_slot_id": "old-slot"}, "nonce-1", 12144) is False


def test_panel_host_config_preserves_all_runtime_project_profiles(tmp_path: Path):
    runtime_config = {
        "port": 8770,
        "runtime_root": str(tmp_path / "state"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {"project_id": "lari", "workspace": "lari-ws"},
            "lari-ui-v2": {"project_id": "lari-ui-v2", "workspace": "ui-ws"},
        },
        "default_project": "lari",
        "authoritative_repo_path": str(tmp_path / "authoritative"),
        "operations_repo_path": str(tmp_path / "operations"),
        "operations_ref": "refs/heads/operations",
        "candidate_source_sha": "a" * 40,
    }
    (tmp_path / "runtime-config.json").write_text(json.dumps(runtime_config), encoding="utf-8")
    supervisor_config = tmp_path / "supervisor-config.json"
    supervisor_config.write_text(json.dumps({
        "supervisor_root": str(tmp_path / "supervisor"),
        "runtime_config_path": str(tmp_path / "runtime-config.json"),
        "panel_host_config_path": str(tmp_path / "panel-host.json"),
        "panel_config_path": str(tmp_path / "panel.json"),
    }), encoding="utf-8")
    supervisor = RuntimeSupervisor(supervisor_config)

    host_path, _ = supervisor._ensure_panel_configs()
    host = json.loads(host_path.read_text(encoding="utf-8"))

    assert host["default_project_id"] == "lari"
    assert set(host["projects"]) == {"lari", "lari-ui-v2"}
    assert host["authoritative_repo_path"] == str(tmp_path / "authoritative")
    assert host["operations_repo_path"] == str(tmp_path / "operations")
    assert host["operations_ref"] == "refs/heads/operations"
    assert host["candidate_source_sha"] == "a" * 40


def test_panel_host_config_does_not_invent_repository_authority(tmp_path: Path):
    (tmp_path / "runtime-config.json").write_text(json.dumps({
        "port": 8770,
        "runtime_root": str(tmp_path / "state"),
        "authorized_roots": [str(tmp_path)],
        "projects": {},
        "default_project": None,
    }), encoding="utf-8")
    supervisor_config = tmp_path / "supervisor-config.json"
    supervisor_config.write_text(json.dumps({
        "supervisor_root": str(tmp_path / "supervisor"),
        "runtime_config_path": str(tmp_path / "runtime-config.json"),
        "panel_host_config_path": str(tmp_path / "panel-host.json"),
        "panel_config_path": str(tmp_path / "panel.json"),
    }), encoding="utf-8")

    host_path, _ = RuntimeSupervisor(supervisor_config)._ensure_panel_configs()
    host = json.loads(host_path.read_text(encoding="utf-8"))

    assert "operations_repo_path" not in host
    assert "authoritative_repo_path" not in host


def test_panel_child_uses_runtime_home_cwd_not_hostile_caller_cwd(
    tmp_path: Path,
    monkeypatch,
):
    runtime_home = tmp_path / "runtime-v1"
    runtime_home.mkdir()
    runtime_config = runtime_home / "runtime-config.json"
    runtime_config.write_text(json.dumps({
        "port": 18770,
        "runtime_root": str(runtime_home / "state"),
        "authorized_roots": [str(runtime_home)],
        "projects": {},
        "default_project": None,
    }), encoding="utf-8")
    supervisor_config = runtime_home / "supervisor-config.json"
    supervisor_config.write_text(json.dumps({
        "supervisor_root": str(runtime_home / "supervisor"),
        "runtime_config_path": str(runtime_config),
        "panel_host_config_path": str(runtime_home / "control-panel-host-config.json"),
        "panel_config_path": str(runtime_home / "control-panel-config.json"),
    }), encoding="utf-8")

    hostile_cwd = tmp_path / "hostile-cwd"
    stale_extensions = hostile_cwd / "extensions"
    stale_extensions.mkdir(parents=True)
    (stale_extensions / "__init__.py").write_text(
        "# stale checkout package\n",
        encoding="utf-8",
    )
    candidate_site = str(tmp_path / "candidate" / "site")
    monkeypatch.chdir(hostile_cwd)
    monkeypatch.setenv("PYTHONPATH", candidate_site)

    captured = {}

    def fake_popen_headless(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(runtime_supervisor_module, "popen_headless", fake_popen_headless)
    supervisor = RuntimeSupervisor(supervisor_config)
    monkeypatch.setattr(supervisor, "_panel_health", lambda: None)

    assert supervisor._ensure_panel("a" * 40) is False
    assert captured["cwd"] == str(supervisor_config.parent.resolve())
    assert captured["cwd"] != str(hostile_cwd.resolve())
    assert captured["env"]["PYTHONPATH"] == candidate_site
    assert captured["env"]["AG_BACKEND_ENABLED"] == "FALSE"
    assert captured["env"]["AOS_RUNTIME_SOURCE_SHA"] == "a" * 40


def _panel_supervisor(tmp_path: Path) -> RuntimeSupervisor:
    runtime_config = tmp_path / "runtime-config.json"
    runtime_config.write_text(json.dumps({
        "port": 18770,
        "runtime_root": str(tmp_path / "state"),
        "authorized_roots": [str(tmp_path)],
        "projects": {},
        "default_project": None,
    }), encoding="utf-8")
    supervisor_config = tmp_path / "supervisor-config.json"
    supervisor_config.write_text(json.dumps({
        "supervisor_root": str(tmp_path / "supervisor"),
        "runtime_config_path": str(runtime_config),
        "panel_host_config_path": str(tmp_path / "panel-host.json"),
        "panel_config_path": str(tmp_path / "panel.json"),
    }), encoding="utf-8")
    return RuntimeSupervisor(supervisor_config)


def test_supervisor_reuses_healthy_owned_panel_only_for_exact_source_sha(
    tmp_path: Path, monkeypatch
):
    supervisor = _panel_supervisor(tmp_path)
    panel_pid = 41234
    monkeypatch.setattr(supervisor, "_panel_health", lambda: {
        "panel_state": "HEALTHY",
        "supervisor_pid": os.getpid(),
        "pid": panel_pid,
        "runtime_source_sha": "a" * 40,
    })
    monkeypatch.setattr(
        runtime_supervisor_module,
        "_terminate_pid",
        lambda _pid: pytest.fail("matching panel must not be terminated"),
    )
    monkeypatch.setattr(
        runtime_supervisor_module,
        "popen_headless",
        lambda *_args, **_kwargs: pytest.fail("matching panel must not be relaunched"),
    )

    assert supervisor._ensure_panel("a" * 40) is True
    assert supervisor.panel_api_pid == panel_pid


def test_supervisor_does_not_reuse_owned_panel_with_invalid_panel_pid(
    tmp_path: Path, monkeypatch
):
    supervisor = _panel_supervisor(tmp_path)
    monkeypatch.setattr(supervisor, "_panel_health", lambda: {
        "panel_state": "HEALTHY",
        "supervisor_pid": os.getpid(),
        "pid": os.getpid(),
        "runtime_source_sha": "a" * 40,
    })
    monkeypatch.setattr(
        runtime_supervisor_module,
        "popen_headless",
        lambda *_args, **_kwargs: pytest.fail("invalid identity must fail closed"),
    )

    assert supervisor._ensure_panel("a" * 40) is False
    assert supervisor.panel_api_pid is None


def test_supervisor_replaces_owned_healthy_panel_with_stale_source_sha(
    tmp_path: Path, monkeypatch
):
    supervisor = _panel_supervisor(tmp_path)
    panel_pid = 41235
    terminated = []
    launched = {}
    child = object()
    monkeypatch.setattr(supervisor, "_panel_health", lambda: {
        "panel_state": "HEALTHY",
        "supervisor_pid": os.getpid(),
        "pid": panel_pid,
        "runtime_source_sha": "b" * 40,
    })
    monkeypatch.setattr(
        runtime_supervisor_module, "_terminate_pid", terminated.append
    )

    def fake_launch(command, **kwargs):
        launched["command"] = command
        launched.update(kwargs)
        return child

    monkeypatch.setattr(runtime_supervisor_module, "popen_headless", fake_launch)

    assert supervisor._ensure_panel("a" * 40) is False
    assert terminated == [panel_pid]
    assert supervisor.panel_child is child
    assert launched["env"]["AOS_RUNTIME_SOURCE_SHA"] == "a" * 40


def test_supervisor_fails_closed_for_healthy_panel_owned_by_live_other_process(
    tmp_path: Path, monkeypatch
):
    supervisor = _panel_supervisor(tmp_path)
    other_owner = os.getpid() + 1000
    panel_pid = 41236
    terminated = []
    monkeypatch.setattr(supervisor, "_panel_health", lambda: {
        "panel_state": "HEALTHY",
        "supervisor_pid": other_owner,
        "pid": panel_pid,
        "runtime_source_sha": "b" * 40,
    })
    monkeypatch.setattr(runtime_supervisor_module, "pid_alive", lambda pid: pid == other_owner)
    monkeypatch.setattr(
        runtime_supervisor_module, "_terminate_pid", terminated.append
    )
    monkeypatch.setattr(
        runtime_supervisor_module,
        "popen_headless",
        lambda *_args, **_kwargs: pytest.fail("foreign panel port must fail closed"),
    )

    assert supervisor._ensure_panel("a" * 40) is False
    assert terminated == []
    assert supervisor.panel_child is None
