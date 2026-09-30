import json
import os
import sys
from pathlib import Path

import pytest

import aos.runtime_deploy as deploy
import aos.runtime_slots as runtime_slots
from aos.knowledge.index import derive_index
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.materialize import render_documents
from aos.knowledge.receipts import (
    record_implementation_receipt,
    record_live_promotion_receipt,
    record_verification_receipt,
)
from aos.knowledge.runtime_transitions import prepare_transition
from aos.runtime_deploy import activate, reconcile_runtime_transitions, rollback
from aos.runtime_slots import RuntimeTransitionIncompleteError, SlotManager, SlotRecord
from aos.runtime_store import atomic_json, read_json


SOURCE_SHA = "d" * 40
STABLE_SHA = "c" * 40


def _accepted(ledger: KnowledgeLedger, sha: str) -> None:
    record_implementation_receipt(
        ledger,
        project_id="AOS",
        idempotency_key=f"impl:{sha}",
        agent_class="CODEX",
        tool_name="pytest",
        base_sha="a" * 40,
        result_sha=sha,
    )
    record_verification_receipt(
        ledger,
        project_id="AOS",
        idempotency_key=f"verify:{sha}",
        agent_class="CODEX",
        tool_name="pytest",
        result_sha=sha,
        verification={"status": "PASS"},
    )


def _slot_environment(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    manager = SlotManager(tmp_path / "supervisor", knowledge_ledger=ledger)
    stable = SlotRecord("stable", "runtime_v1", ("python", "stable.py"), STABLE_SHA, None, None, "now")
    candidate = SlotRecord("candidate", "runtime_v1", ("python", "candidate.py"), SOURCE_SHA, None, None, "now")
    manager.write_slot(stable)
    manager.write_slot(candidate)
    manager.initialize(stable_slot_id="stable", candidate_slot_id="candidate", active="candidate")
    _accepted(ledger, SOURCE_SHA)
    return ledger, manager


def _types(ledger: KnowledgeLedger):
    return [event["event_type"] for event in ledger.read_events()]


def test_promotion_prepare_failure_leaves_pointer_unchanged(tmp_path, monkeypatch):
    ledger, manager = _slot_environment(tmp_path)
    before = read_json(manager.pointer, {})
    monkeypatch.setattr(runtime_slots, "prepare_transition", lambda *a, **k: (_ for _ in ()).throw(OSError("prepare")))
    with pytest.raises(OSError, match="prepare"):
        manager.promote_candidate(proof_id="proof")
    assert read_json(manager.pointer, {}) == before
    assert "LIVE_PROMOTION_RECEIPT" not in _types(ledger)


def test_promotion_failure_after_prepare_aborts_without_completion(tmp_path, monkeypatch):
    ledger, manager = _slot_environment(tmp_path)
    before = read_json(manager.pointer, {})
    real_atomic = runtime_slots.atomic_json
    failed = False

    def fail_pointer_once(path, value):
        nonlocal failed
        if Path(path) == manager.pointer and value.get("transition_state") and not failed:
            failed = True
            raise OSError("pointer commit")
        return real_atomic(path, value)

    monkeypatch.setattr(runtime_slots, "atomic_json", fail_pointer_once)
    with pytest.raises(OSError, match="pointer commit"):
        manager.promote_candidate(proof_id="proof")
    assert read_json(manager.pointer, {}) == before
    assert "RUNTIME_TRANSITION_INTENT" in _types(ledger)
    assert "RUNTIME_TRANSITION_ABORTED" in _types(ledger)
    assert "LIVE_PROMOTION_RECEIPT" not in _types(ledger)


def test_promotion_completion_failure_restores_previous_state(tmp_path, monkeypatch):
    ledger, manager = _slot_environment(tmp_path)
    before = read_json(manager.pointer, {})
    monkeypatch.setattr(runtime_slots, "record_live_promotion_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("receipt")))
    with pytest.raises(OSError, match="receipt"):
        manager.promote_candidate(proof_id="proof")
    assert read_json(manager.pointer, {}) == before
    assert manager.active_slot().slot_id == "candidate"
    assert "LIVE_PROMOTION_RECEIPT" not in _types(ledger)


def test_promotion_success_orders_intent_before_completion(tmp_path):
    ledger, manager = _slot_environment(tmp_path)
    promoted = manager.promote_candidate(proof_id="proof")
    events = ledger.read_events()
    intent = next(event for event in events if event["event_type"] == "RUNTIME_TRANSITION_INTENT")
    completion = next(event for event in events if event["event_type"] == "LIVE_PROMOTION_RECEIPT")
    assert intent["sequence"] < completion["sequence"]
    assert intent["claims"]["transition_id"] == completion["claims"]["transition_id"]
    assert promoted["promotion_state"] == "STABLE"
    assert promoted["stable_slot_id"] == "candidate"
    assert manager.active_slot().source_sha == SOURCE_SHA


def test_slot_rollback_prepare_and_completion_failures_are_safe(tmp_path, monkeypatch):
    ledger, manager = _slot_environment(tmp_path)
    before = read_json(manager.pointer, {})
    real_prepare = runtime_slots.prepare_transition
    monkeypatch.setattr(runtime_slots, "prepare_transition", lambda *a, **k: (_ for _ in ()).throw(OSError("prepare")))
    with pytest.raises(OSError, match="prepare"):
        manager.rollback(reason="failed health")
    assert read_json(manager.pointer, {}) == before

    monkeypatch.setattr(runtime_slots, "prepare_transition", real_prepare)
    monkeypatch.setattr(runtime_slots, "record_rollback_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("receipt")))
    with pytest.raises(OSError, match="receipt"):
        manager.rollback(reason="failed health")
    assert read_json(manager.pointer, {}) == before
    assert "ROLLBACK_RECEIPT" not in _types(ledger)


def test_slot_rollback_success_has_durable_completion(tmp_path):
    ledger, manager = _slot_environment(tmp_path)
    result = manager.rollback(reason="failed health")
    completion = next(event for event in ledger.read_events() if event["event_type"] == "ROLLBACK_RECEIPT")
    assert result["active"] == "stable"
    assert completion["claims"]["restored_slot_id"] == "stable"


def _deploy_environment(tmp_path: Path, monkeypatch):
    runtime_home = tmp_path / "runtime-home"
    candidate = runtime_home / "candidate" / SOURCE_SHA
    candidate.mkdir(parents=True)
    (candidate / "launch_supervisor.py").write_text("pass\n", encoding="utf-8")
    atomic_json(candidate / "candidate-manifest.json", {
        "candidate_source_sha": SOURCE_SHA,
        "build_source_sha": SOURCE_SHA,
        "candidate_slot_id": f"candidate-{SOURCE_SHA[:8]}",
        "candidate_tree_sha256": "e" * 64,
        "ci_run_id": 123,
    })
    runtime_root = runtime_home / "state"
    config_path = runtime_home / "runtime-config.json"
    atomic_json(config_path, {
        "contract_version": "1.0.0",
        "runtime_root": str(runtime_root),
        "runtime_token_path": str(runtime_home / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {},
        "port": 18770,
        "production": "NO_GO",
        "ag_backend_enabled": False,
    })
    supervisor = runtime_home / "supervisor"
    atomic_json(runtime_home / "supervisor-config.json", {
        "contract_version": "1.0.0",
        "supervisor_root": str(supervisor),
        "runtime_config_path": str(config_path),
        "production": "NO_GO",
    })
    slots = SlotManager(supervisor)
    stable = SlotRecord(
        "stable", "runtime_v1", (sys.executable, "stable.py"), STABLE_SHA,
        None, str(config_path), "now",
    )
    slots.write_slot(stable)
    slots.initialize(stable_slot_id="stable", candidate_slot_id="stable", active="stable")
    ledger = deploy.ledger_for_runtime(runtime_home)
    _accepted(ledger, SOURCE_SHA)
    monkeypatch.setattr(deploy, "validate", lambda *a, **k: {
        "validation": "PASS", "candidate": str(candidate), "production": "NO_GO"
    })
    monkeypatch.setattr(
        deploy,
        "_candidate_runtime_config",
        lambda config, candidate_path, manifest: {
            **config,
            "candidate_source_sha": manifest["candidate_source_sha"],
            "build_source_sha": manifest["build_source_sha"],
            "runtime_slot_id": manifest["candidate_slot_id"],
            "runtime_slot_root": str(candidate_path),
            "runtime_asset_tree_sha256": manifest["candidate_tree_sha256"],
            "production": "NO_GO",
        },
    )
    return runtime_home, tmp_path / "Startup", supervisor, ledger


def test_activation_prepare_failure_has_no_runtime_mutation(tmp_path, monkeypatch):
    runtime_home, startup, supervisor, ledger = _deploy_environment(tmp_path, monkeypatch)
    config_before = read_json(runtime_home / "runtime-config.json", {})
    pointer_before = read_json(supervisor / "active-slot.json", {})
    monkeypatch.setattr(deploy, "prepare_transition", lambda *a, **k: (_ for _ in ()).throw(OSError("prepare")))
    with pytest.raises(OSError, match="prepare"):
        activate(runtime_home, SOURCE_SHA, startup, launch=False)
    assert read_json(runtime_home / "runtime-config.json", {}) == config_before
    assert read_json(supervisor / "active-slot.json", {}) == pointer_before
    assert not (supervisor / "control-request.json").exists()
    assert not (startup / "AOS-Runtime-V1-Supervisor.vbs").exists()
    assert "RUNTIME_TRANSITION_INTENT" not in _types(ledger)


@pytest.mark.parametrize("boundary", ["runtime_config", "slot", "pointer", "control", "startup"])
def test_activation_failure_after_each_mutation_restores_or_holds(tmp_path, monkeypatch, boundary):
    runtime_home, startup, supervisor, ledger = _deploy_environment(tmp_path, monkeypatch)
    config_before = read_json(runtime_home / "runtime-config.json", {})
    pointer_before = read_json(supervisor / "active-slot.json", {})
    real_atomic = deploy.atomic_json
    real_write_slot = SlotManager.write_slot
    real_install = deploy._install_startup
    failed = False

    def fail_atomic_once(path, value):
        nonlocal failed
        path = Path(path)
        matches = (
            (boundary == "runtime_config" and path == runtime_home / "runtime-config.json" and value.get("candidate_source_sha") == SOURCE_SHA)
            or (boundary == "pointer" and path == supervisor / "active-slot.json" and value.get("transition_state") == "INCOMPLETE_HOLD")
            or (boundary == "control" and path == supervisor / "control-request.json" and value.get("action") == "START")
        )
        result = real_atomic(path, value)
        if matches and not failed:
            failed = True
            raise OSError(boundary)
        return result

    def fail_slot_once(self, record):
        nonlocal failed
        result = real_write_slot(self, record)
        if boundary == "slot" and not failed:
            failed = True
            raise OSError(boundary)
        return result

    def fail_startup_once(*args, **kwargs):
        nonlocal failed
        result = real_install(*args, **kwargs)
        if boundary == "startup" and not failed:
            failed = True
            raise OSError(boundary)
        return result

    monkeypatch.setattr(deploy, "atomic_json", fail_atomic_once)
    monkeypatch.setattr(SlotManager, "write_slot", fail_slot_once)
    monkeypatch.setattr(deploy, "_install_startup", fail_startup_once)
    with pytest.raises(OSError, match=boundary):
        activate(runtime_home, SOURCE_SHA, startup, launch=False, install_startup=boundary == "startup")
    assert failed
    assert read_json(runtime_home / "runtime-config.json", {}) == config_before
    assert read_json(supervisor / "active-slot.json", {}) == pointer_before
    assert not (startup / "AOS-Runtime-V1-Supervisor.vbs").exists()
    assert "LIVE_PROMOTION_RECEIPT" not in _types(ledger)
    assert "RUNTIME_TRANSITION_ABORTED" in _types(ledger)


def test_activation_completion_failure_restores_candidate(tmp_path, monkeypatch):
    runtime_home, startup, supervisor, ledger = _deploy_environment(tmp_path, monkeypatch)
    monkeypatch.setattr(deploy, "record_live_promotion_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("receipt")))
    with pytest.raises(OSError, match="receipt"):
        activate(runtime_home, SOURCE_SHA, startup, launch=False, install_startup=False)
    pointer = read_json(supervisor / "active-slot.json", {})
    assert pointer["active"] == "stable"
    assert pointer.get("transition_state") is None
    assert "LIVE_PROMOTION_RECEIPT" not in _types(ledger)


def test_incomplete_activation_reconciles_after_restart(tmp_path, monkeypatch):
    runtime_home, startup, supervisor, ledger = _deploy_environment(tmp_path, monkeypatch)
    real_restore = deploy._restore_transaction
    monkeypatch.setattr(deploy, "record_live_promotion_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("receipt")))
    monkeypatch.setattr(deploy, "_restore_transaction", lambda *a, **k: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(deploy.DeploymentError, match="automatic rollback also failed"):
        activate(runtime_home, SOURCE_SHA, startup, launch=False, install_startup=False)
    assert read_json(supervisor / "active-slot.json", {})["transition_state"] == "INCOMPLETE_HOLD"
    monkeypatch.setattr(deploy, "_restore_transaction", real_restore)
    result = reconcile_runtime_transitions(runtime_home, startup)
    assert result["reconciled"][0]["result"] == "ABORTED"
    assert read_json(supervisor / "active-slot.json", {})["active"] == "stable"
    assert "RUNTIME_TRANSITION_ABORTED" in _types(ledger)


def test_exact_live_owner_can_use_only_its_prepared_activation(tmp_path):
    ledger, manager = _slot_environment(tmp_path)
    pointer = read_json(manager.pointer, {})
    transition_id = "f" * 64
    prepare_transition(
        ledger,
        transition_id=transition_id,
        operation="ACTIVATE",
        boundary="aos.runtime_deploy",
        resource="transaction.json",
        result_sha=SOURCE_SHA,
        previous_state={"slot_pointer": pointer},
        target_state={"slot_pointer": pointer},
        module_ids=["RuntimeDeploy", "RuntimeSupervisor"],
    )
    atomic_json(manager.pointer, {
        **pointer,
        "transition_state": "INCOMPLETE_HOLD",
        "transition_id": transition_id,
        "transition_operation": "ACTIVATE",
    })
    provisional = SlotManager(
        manager.root,
        knowledge_ledger=ledger,
        prepared_transition_id=transition_id,
        prepared_transition_owner_pid=os.getpid(),
    )
    assert provisional.read_pointer()["transition_id"] == transition_id
    with pytest.raises(RuntimeTransitionIncompleteError):
        SlotManager(manager.root, knowledge_ledger=ledger).read_pointer()


def test_runtime_rollback_prepare_failure_does_not_restore(tmp_path, monkeypatch):
    runtime_home, startup, supervisor, _ = _deploy_environment(tmp_path, monkeypatch)
    activated = activate(runtime_home, SOURCE_SHA, startup, launch=False, install_startup=False)
    pointer_before = read_json(supervisor / "active-slot.json", {})
    monkeypatch.setattr(deploy, "prepare_transition", lambda *a, **k: (_ for _ in ()).throw(OSError("prepare")))
    with pytest.raises(OSError, match="prepare"):
        rollback(runtime_home, activated["transaction_id"], startup)
    assert read_json(supervisor / "active-slot.json", {}) == pointer_before


def test_runtime_rollback_completion_failure_holds_then_reconciles(tmp_path, monkeypatch):
    runtime_home, startup, supervisor, ledger = _deploy_environment(tmp_path, monkeypatch)
    activated = activate(runtime_home, SOURCE_SHA, startup, launch=False, install_startup=False)
    real_receipt = deploy.record_rollback_receipt
    monkeypatch.setattr(deploy, "record_rollback_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError("receipt")))
    with pytest.raises(OSError, match="receipt"):
        rollback(runtime_home, activated["transaction_id"], startup)
    held = read_json(supervisor / "active-slot.json", {})
    assert held["transition_state"] == "INCOMPLETE_HOLD"
    with pytest.raises(RuntimeTransitionIncompleteError):
        SlotManager(supervisor, knowledge_ledger=ledger).read_pointer()
    monkeypatch.setattr(deploy, "record_rollback_receipt", real_receipt)
    result = reconcile_runtime_transitions(runtime_home, startup)
    assert any(item["result"] == "COMPLETED" for item in result["reconciled"])
    assert read_json(supervisor / "active-slot.json", {})["active"] == "stable"
    assert "ROLLBACK_RECEIPT" in _types(ledger)


def test_transition_projection_never_treats_intent_as_live_promotion(tmp_path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    prepare_transition(
        ledger,
        transition_id="t" * 64,
        operation="PROMOTE",
        boundary="aos.runtime_slots",
        resource="active-slot.json",
        result_sha=SOURCE_SHA,
        previous_state={"active": "candidate"},
        target_state={"active": "stable"},
        module_ids=["RuntimeSupervisor"],
    )
    index = derive_index(ledger.read_events())
    assert index["latest_live_promotion"] is None
    assert "t" * 64 in index["unresolved_runtime_transitions"]
    live = render_documents(ledger)["AOS_LIVE_AND_VERIFICATION_HISTORY.md"]
    assert "Prepared or Incomplete Runtime Transitions" in live

    record_live_promotion_receipt(
        ledger,
        project_id="AOS",
        idempotency_key="complete:t",
        agent_class="AOS_NATIVE",
        tool_name="pytest",
        result_sha=SOURCE_SHA,
        claims={"transition_id": "t" * 64, "promotion_state": "STABLE"},
    )
    completed = derive_index(ledger.read_events())
    assert completed["latest_live_promotion"]["event_type"] == "LIVE_PROMOTION_RECEIPT"
    assert completed["unresolved_runtime_transitions"] == {}
