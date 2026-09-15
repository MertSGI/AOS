from aos.runtime_slots import SlotRecord
from aos.runtime_supervisor import _runtime_health_matches


def _slot():
    return SlotRecord(
        "candidate-runtime-v1.4-abc",
        "runtime_v1",
        ("python", "runtime.py"),
        "a" * 40,
        "http://127.0.0.1:8770/v1/health",
        "runtime-config.json",
        "now",
    )


def test_runtime_supervisor_requires_child_pid_exact_sha_and_slot_identity():
    slot = _slot()
    good = {
        "pid": 1234,
        "runtime_source_sha": "a" * 40,
        "runtime_slot_id": slot.slot_id,
    }
    assert _runtime_health_matches(slot, 1234, good) is True
    assert _runtime_health_matches(slot, 9999, good) is False
    assert _runtime_health_matches(slot, 1234, {**good, "runtime_source_sha": "b" * 40}) is False
    assert _runtime_health_matches(slot, 1234, {**good, "runtime_slot_id": "old-slot"}) is False
    assert _runtime_health_matches(slot, 1234, {"pid": 1234}) is False
