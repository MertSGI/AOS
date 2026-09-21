import json
import sys
from pathlib import Path

import materialize_slot
import pytest

from aos.runtime_deploy import DeploymentError, activate, rollback, validate, validate_startup_ownership
from aos.runtime_maintenance import PAUSED_SAFE, read_maintenance
from aos.runtime_slots import SlotManager, SlotRecord
from aos.runtime_store import atomic_json


ROOT = Path(__file__).resolve().parent.parent
BASE_SHA = "ab1e1d248820dd10b7900f8117cd1e0b48687be3"


def test_transactional_activation_defaults_paused_and_rolls_back(tmp_path: Path, monkeypatch):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setattr(materialize_slot, "verify_ci_run", lambda *args, **kwargs: {"conclusion": "success"})
    monkeypatch.setattr(materialize_slot, "assert_clean_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(materialize_slot, "get_authoritative_git_head", lambda _: BASE_SHA)
    candidate = materialize_slot.materialize(
        BASE_SHA,
        35578238176,
        repo_root=ROOT,
        candidate_base=runtime_home / "candidate",
    )
    validation = validate(runtime_home, BASE_SHA)
    assert validation["validation"] == "PASS"
    assert all(str(candidate.resolve()) in origin for origin in validation["import_origins"].values())

    runtime_root = runtime_home / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    atomic_json(runtime_home / "runtime-config.json", {
        "contract_version": "1.0.0",
        "runtime_root": str(runtime_root),
        "runtime_token_path": str(runtime_home / "runtime-api.token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(candidate / "descriptors" / "lari.autonomous-host.descriptor.json"),
                "routing_policy_path": str(candidate / "descriptors" / "nemotron.planner-policy.json"),
                "workspace": str(workspace),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "port": 18770,
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })
    supervisor_root = runtime_home / "supervisor"
    atomic_json(runtime_home / "supervisor-config.json", {
        "contract_version": "1.0.0",
        "supervisor_root": str(supervisor_root),
        "production": "NO_GO",
        "panel_enabled": False,
    })
    slots = SlotManager(supervisor_root)
    stable = SlotRecord(
        slot_id="stable-accepted",
        kind="runtime_v1",
        command=(sys.executable, str(candidate / "launch_runtime_server.py")),
        source_sha=BASE_SHA,
        health_url="http://127.0.0.1:18770/v1/health",
        config_path=str(runtime_home / "runtime-config.json"),
        created_at="2026-09-21T00:00:00Z",
    )
    slots.write_slot(stable)
    slots.initialize(stable_slot_id=stable.slot_id, candidate_slot_id=stable.slot_id, active="stable")

    startup = tmp_path / "Startup"
    result = activate(runtime_home, BASE_SHA, startup, launch=False)
    assert result["activation"] == "STAGED_MAINTENANCE"
    assert read_maintenance(runtime_root)["state"] == PAUSED_SAFE
    pointer = slots.read_pointer()
    assert pointer["active"] == "candidate"
    assert pointer["stable_slot_id"] == stable.slot_id
    startup_file = startup / "AOS-Runtime-V1-Supervisor.pyw"
    startup_text = startup_file.read_text(encoding="utf-8")
    assert BASE_SHA in startup_text
    assert "launch_supervisor.py" in startup_text

    restored = rollback(runtime_home, result["transaction_id"], startup)
    assert restored["rollback"] == "PASS"
    assert slots.read_pointer()["active"] == "stable"


def test_startup_validation_rejects_duplicate_authorities(tmp_path: Path):
    startup = tmp_path / "Startup"
    startup.mkdir()
    (startup / "AOS-Runtime-V1-Supervisor.cmd").write_text("exit /b 0", encoding="utf-8")
    (startup / "AOS-Runtime-V1-Supervisor.pyw").write_text("pass", encoding="utf-8")
    with pytest.raises(DeploymentError, match="Duplicate enabled AOS startup authorities"):
        validate_startup_ownership(startup, startup / "AOS-Runtime-V1-Supervisor.pyw")

