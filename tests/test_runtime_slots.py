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
