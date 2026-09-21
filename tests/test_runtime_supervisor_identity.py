import json
from pathlib import Path

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
