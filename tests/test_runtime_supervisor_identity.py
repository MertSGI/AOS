from aos.runtime_slots import SlotRecord
from aos.runtime_supervisor import _runtime_health_matches


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
