import json
import sys
from pathlib import Path

import materialize_slot
import pytest

import aos.runtime_deploy as runtime_deploy
from aos.runtime_deploy import DeploymentError, activate, rollback, validate, validate_startup_ownership
from aos.runtime_maintenance import PAUSED_SAFE, read_maintenance
from aos.runtime_slots import SlotManager, SlotRecord
from aos.knowledge.hooks import ledger_for_runtime
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt
from aos.runtime_store import atomic_json
from aos.validate import validate_file


ROOT = Path(__file__).resolve().parent.parent
BASE_SHA = "ab1e1d248820dd10b7900f8117cd1e0b48687be3"


def _record_accepted_source(runtime_home: Path) -> None:
    ledger = ledger_for_runtime(runtime_home)
    record_implementation_receipt(
        ledger, project_id="AOS", idempotency_key="runtime-deploy-test-impl",
        agent_class="CODEX", tool_name="pytest", base_sha="a" * 40,
        result_sha=BASE_SHA, changed_paths=["src/aos/runtime_deploy.py"],
    )
    record_verification_receipt(
        ledger, project_id="AOS", idempotency_key="runtime-deploy-test-verify",
        agent_class="CODEX", tool_name="pytest", result_sha=BASE_SHA,
        changed_paths=["src/aos/runtime_deploy.py"], verification={"status": "PASS"},
    )


def test_candidate_validation_rebinds_maintenance_descriptor(tmp_path: Path, monkeypatch):
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setattr(
        materialize_slot,
        "verify_ci_run",
        lambda *args, **kwargs: {"conclusion": "success"},
    )
    monkeypatch.setattr(materialize_slot, "assert_clean_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(materialize_slot, "get_authoritative_git_head", lambda _: BASE_SHA)
    candidate = materialize_slot.materialize(
        BASE_SHA,
        35578238176,
        repo_root=ROOT,
        candidate_base=runtime_home / "candidate",
    )

    workspace = tmp_path / "maintenance-workspace"
    workspace.mkdir()
    live_descriptor = (
        tmp_path
        / "live-profile"
        / "descriptors"
        / "aos-maintenance.autonomous-host.descriptor.json"
    )
    live_policy = tmp_path / "live-profile" / "descriptors" / "nemotron.planner-policy.json"
    atomic_json(runtime_home / "runtime-config.json", {
        "contract_version": "1.0.0",
        "runtime_root": str(runtime_home / "state"),
        "runtime_token_path": str(runtime_home / "runtime-api.token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "aos-maintenance": {
                "project_id": "aos-maintenance",
                "descriptor_path": str(live_descriptor),
                "routing_policy_path": str(live_policy),
                "workspace": str(workspace),
                "standing_authority": True,
            }
        },
        "default_project": "aos-maintenance",
        "port": 18770,
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })

    captured = {}
    validate_profiles = runtime_deploy.validate_configured_project_profiles

    def capture_validated_config(config):
        normalized = validate_profiles(config)
        captured.update(normalized)
        return normalized

    monkeypatch.setattr(
        runtime_deploy,
        "validate_configured_project_profiles",
        capture_validated_config,
    )
    result = runtime_deploy.validate(runtime_home, BASE_SHA)

    candidate_descriptor = (
        candidate / "descriptors" / "aos-maintenance.autonomous-host.descriptor.json"
    )
    candidate_policy = candidate / "descriptors" / "nemotron.planner-policy.json"
    assert live_descriptor.name == candidate_descriptor.name
    assert candidate_descriptor.is_file()
    descriptor_result, descriptor_code = validate_file(
        "project_descriptor", candidate_descriptor
    )
    assert descriptor_code == 0, [error.message for error in descriptor_result.errors]
    descriptor = json.loads(candidate_descriptor.read_text(encoding="utf-8"))
    assert descriptor["project_id"] == "aos-maintenance"
    policy_result, policy_code = validate_file("planner_routing_policy", candidate_policy)
    assert policy_code == 0, [error.message for error in policy_result.errors]

    rebound = captured["projects"]["aos-maintenance"]
    assert Path(rebound["descriptor_path"]).resolve() == candidate_descriptor.resolve()
    assert Path(rebound["routing_policy_path"]).resolve() == candidate_policy.resolve()
    assert Path(rebound["workspace"]).resolve() == workspace.resolve()
    assert rebound["project_id"] == "aos-maintenance"
    assert captured["production"] == "NO_GO"
    assert result["validation"] == "PASS"
    assert result["production"] == "NO_GO"


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
    _record_accepted_source(runtime_home)
    result = activate(runtime_home, BASE_SHA, startup, launch=False)
    assert result["activation"] == "STAGED_MAINTENANCE"
    assert read_maintenance(runtime_root)["state"] == PAUSED_SAFE
    pointer = slots.read_pointer()
    assert pointer["active"] == "candidate"
    assert pointer["stable_slot_id"] == stable.slot_id
    startup_file = startup / "AOS-Runtime-V1-Supervisor.vbs"
    startup_text = startup_file.read_text(encoding="utf-8")
    assert BASE_SHA in startup_text
    assert "WScript.Shell" in startup_text
    assert "shell.Run" in startup_text
    assert "launch_supervisor.py" in startup_text

    restored = rollback(runtime_home, result["transaction_id"], startup)
    assert restored["rollback"] == "PASS"
    assert slots.read_pointer()["active"] == "stable"
    assert not startup_file.exists()

    # Maintenance-only deployment must be able to stage the exact candidate
    # while leaving Startup completely untouched.
    no_startup = activate(
        runtime_home,
        BASE_SHA,
        startup,
        launch=False,
        install_startup=False,
    )
    assert no_startup["activation"] == "STAGED_MAINTENANCE_NO_STARTUP"
    assert no_startup["startup"] is None
    assert no_startup["startup_installed"] is False
    assert not startup_file.exists()
    assert read_maintenance(runtime_root)["state"] == PAUSED_SAFE

    tx = json.loads(
        (
            runtime_home
            / "deployment-backups"
            / no_startup["transaction_id"]
            / "transaction.json"
        ).read_text(encoding="utf-8")
    )
    assert tx["startup_managed"] is False

    restored = rollback(
        runtime_home,
        no_startup["transaction_id"],
        startup,
    )
    assert restored["rollback"] == "PASS"
    assert slots.read_pointer()["active"] == "stable"
    assert not startup_file.exists()


def test_activation_rejects_nested_trial_candidate(tmp_path: Path, monkeypatch):
    runtime_home = tmp_path / "runtime-home"

    monkeypatch.setattr(
        materialize_slot,
        "verify_ci_run",
        lambda *args, **kwargs: {"conclusion": "success"},
    )
    monkeypatch.setattr(
        materialize_slot,
        "assert_clean_source",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        materialize_slot,
        "get_authoritative_git_head",
        lambda _: BASE_SHA,
    )

    candidate = materialize_slot.materialize(
        BASE_SHA,
        35578238176,
        repo_root=ROOT,
        candidate_base=runtime_home / "candidate",
    )

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
                "descriptor_path": str(
                    candidate
                    / "descriptors"
                    / "lari.autonomous-host.descriptor.json"
                ),
                "routing_policy_path": str(
                    candidate
                    / "descriptors"
                    / "nemotron.planner-policy.json"
                ),
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

    trial = SlotRecord(
        slot_id="failed-trial",
        kind="runtime_v1",
        command=(sys.executable, str(candidate / "launch_runtime_server.py")),
        source_sha="f" * 40,
        health_url="http://127.0.0.1:18770/v1/health",
        config_path=str(runtime_home / "runtime-config.json"),
        created_at="2026-09-21T00:01:00Z",
    )

    slots.write_slot(stable)
    slots.write_slot(trial)

    atomic_json(slots.pointer, {
        "contract_version": "1.0.0",
        "stable_slot_id": stable.slot_id,
        "candidate_slot_id": trial.slot_id,
        "active": "candidate",
        "promotion_state": "TRIAL",
    })

    startup = tmp_path / "Startup"
    _record_accepted_source(runtime_home)

    with pytest.raises(
        DeploymentError,
        match="Activation requires active=stable",
    ):
        activate(
            runtime_home,
            BASE_SHA,
            startup,
            launch=False,
            install_startup=False,
        )

    pointer = slots.read_pointer()

    assert pointer["active"] == "candidate"
    assert pointer["candidate_slot_id"] == trial.slot_id
    assert pointer["stable_slot_id"] == stable.slot_id
    assert not (
        startup / "AOS-Runtime-V1-Supervisor.vbs"
    ).exists()


def test_startup_validation_rejects_duplicate_authorities(tmp_path: Path):
    startup = tmp_path / "Startup"
    startup.mkdir()
    (startup / "AOS-Runtime-V1-Supervisor.cmd").write_text("exit /b 0", encoding="utf-8")
    (startup / "AOS-Runtime-V1-Supervisor.vbs").write_text(
        'WScript.Quit 0',
        encoding="utf-8",
    )
    with pytest.raises(DeploymentError, match="Duplicate enabled AOS startup authorities"):
        validate_startup_ownership(startup, startup / "AOS-Runtime-V1-Supervisor.pyw")
