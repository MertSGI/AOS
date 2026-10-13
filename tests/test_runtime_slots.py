from pathlib import Path

import pytest

import aos.runtime_slots as runtime_slots
from aos.runtime_slots import SlotManager, SlotRecord
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt


def test_read_pointer_retries_transient_atomic_replace_read(
    tmp_path: Path,
    monkeypatch,
):
    manager = SlotManager(tmp_path / "supervisor")
    expected = {
        "contract_version": "1.0.0",
        "stable_slot_id": "stable-slot",
        "candidate_slot_id": "candidate-slot",
        "active": "candidate",
        "promotion_state": "TRIAL_MAINTENANCE",
    }
    snapshots = iter(({}, expected))
    sleeps = []
    monkeypatch.setattr(runtime_slots, "read_json", lambda _path: next(snapshots))
    monkeypatch.setattr(runtime_slots.time, "sleep", sleeps.append)

    assert manager.read_pointer() == expected
    assert sleeps == [0.02]


def test_read_pointer_fails_closed_after_bounded_invalid_reads(
    tmp_path: Path,
    monkeypatch,
):
    manager = SlotManager(tmp_path / "supervisor")
    sleeps = []
    monkeypatch.setattr(runtime_slots, "read_json", lambda _path: {})
    monkeypatch.setattr(runtime_slots.time, "sleep", sleeps.append)

    with pytest.raises(ValueError, match="Invalid or missing active slot pointer"):
        manager.read_pointer()

    assert sleeps == [0.02, 0.04, 0.06, 0.08]


def test_candidate_slot_rolls_back_atomically_to_stable(tmp_path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    manager = SlotManager(tmp_path / "supervisor", knowledge_ledger=ledger)
    stable = SlotRecord("stable-legacy", "legacy_host", ("python", "-m", "aos.local_host"), "a" * 40, None, None, "now")
    candidate = SlotRecord("candidate-v1", "runtime_v1", ("python", "-m", "aos.runtime_server"), "b" * 40, "http://127.0.0.1:8770/v1/health", None, "now")
    manager.write_slot(stable)
    manager.write_slot(candidate)
    manager.initialize(stable_slot_id=stable.slot_id, candidate_slot_id=candidate.slot_id, active="candidate")
    assert manager.active_slot().slot_id == "candidate-v1"
    pointer = manager.rollback(reason="health failed")
    assert pointer["active"] == "stable"
    assert manager.active_slot().slot_id == "stable-legacy"
    assert pointer["promotion_state"] == "ROLLED_BACK"


def test_candidate_slot_upgrade_and_promotion_flow(tmp_path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    manager = SlotManager(tmp_path / "supervisor", knowledge_ledger=ledger)
    stable = SlotRecord("stable-legacy", "legacy_host", ("python", "-m", "aos.local_host"), "abc", None, None, "now")
    candidate_v1 = SlotRecord("candidate-v1", "runtime_v1", ("python", "-m", "aos.runtime_server"), "a" * 40, "http://127.0.0.1:8770/v1/health", None, "now")
    candidate_v2 = SlotRecord("candidate-v2", "runtime_v1", ("python", "-m", "aos.runtime_server"), "b" * 40, "http://127.0.0.1:8770/v1/health", None, "now")

    manager.write_slot(stable)
    manager.write_slot(candidate_v1)
    manager.initialize(stable_slot_id=stable.slot_id, candidate_slot_id=candidate_v1.slot_id, active="candidate")

    # Mark healthy
    ptr = manager.mark_candidate_healthy()
    assert ptr["candidate_health"] == "HEALTHY"

    # Promote to stable with proof id
    record_implementation_receipt(
        ledger, project_id="AOS", idempotency_key="slot-test-impl",
        agent_class="AOS_NATIVE", tool_name="pytest", base_sha="c" * 40,
        result_sha="a" * 40,
    )
    record_verification_receipt(
        ledger, project_id="AOS", idempotency_key="slot-test-verify",
        agent_class="AOS_NATIVE", tool_name="pytest", result_sha="a" * 40,
        verification={"status": "PASS"},
    )
    promoted = manager.promote_candidate(proof_id="PROOF-FULL-ACCEPTANCE-BATCH-10")
    assert promoted["active"] == "stable"
    assert promoted["stable_slot_id"] == "candidate-v1"
    assert promoted["promotion_state"] == "STABLE"
    assert manager.active_slot().slot_id == "candidate-v1"

    # Upgrade to new candidate v2
    manager.write_slot(candidate_v2)
    manager.initialize(stable_slot_id="candidate-v1", candidate_slot_id=candidate_v2.slot_id, active="candidate")
    assert manager.active_slot().slot_id == "candidate-v2"
    assert manager.read_pointer()["promotion_state"] == "TRIAL"
