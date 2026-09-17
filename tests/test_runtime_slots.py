from aos.runtime_slots import SlotManager, SlotRecord


def test_candidate_slot_rolls_back_atomically_to_stable(tmp_path):
    manager = SlotManager(tmp_path / "supervisor")
    stable = SlotRecord("stable-legacy", "legacy_host", ("python", "-m", "aos.local_host"), "abc", None, None, "now")
    candidate = SlotRecord("candidate-v1", "runtime_v1", ("python", "-m", "aos.runtime_server"), "def", "http://127.0.0.1:8770/v1/health", None, "now")
    manager.write_slot(stable)
    manager.write_slot(candidate)
    manager.initialize(stable_slot_id=stable.slot_id, candidate_slot_id=candidate.slot_id, active="candidate")
    assert manager.active_slot().slot_id == "candidate-v1"
    pointer = manager.rollback(reason="health failed")
    assert pointer["active"] == "stable"
    assert manager.active_slot().slot_id == "stable-legacy"
    assert pointer["promotion_state"] == "ROLLED_BACK"


def test_candidate_slot_upgrade_and_promotion_flow(tmp_path):
    manager = SlotManager(tmp_path / "supervisor")
    stable = SlotRecord("stable-legacy", "legacy_host", ("python", "-m", "aos.local_host"), "abc", None, None, "now")
    candidate_v1 = SlotRecord("candidate-v1", "runtime_v1", ("python", "-m", "aos.runtime_server"), "sha1", "http://127.0.0.1:8770/v1/health", None, "now")
    candidate_v2 = SlotRecord("candidate-v2", "runtime_v1", ("python", "-m", "aos.runtime_server"), "sha2", "http://127.0.0.1:8770/v1/health", None, "now")

    manager.write_slot(stable)
    manager.write_slot(candidate_v1)
    manager.initialize(stable_slot_id=stable.slot_id, candidate_slot_id=candidate_v1.slot_id, active="candidate")

    # Mark healthy
    ptr = manager.mark_candidate_healthy()
    assert ptr["candidate_health"] == "HEALTHY"

    # Promote to stable with proof id
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

