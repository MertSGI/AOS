from aos.runtime_slots import SlotRecord
from aos.runtime_supervisor import _runtime_health_matches


def _slot():
    return SlotRecord("candidate-runtime-v1.5-abc","runtime_v1",("python","runtime.py"),"a"*40,"http://127.0.0.1:8770/v1/health","runtime-config.json","now")


def test_runtime_supervisor_requires_child_pid_exact_sha_slot_and_nonce():
    slot=_slot()
    good={"pid":1234,"runtime_source_sha":"a"*40,"runtime_slot_id":slot.slot_id,"runtime_launch_nonce":"nonce-1"}
    assert _runtime_health_matches(slot,1234,good,"nonce-1") is True
    assert _runtime_health_matches(slot,9999,good,"nonce-1") is False
    assert _runtime_health_matches(slot,1234,{**good,"runtime_source_sha":"b"*40},"nonce-1") is False
    assert _runtime_health_matches(slot,1234,{**good,"runtime_slot_id":"old-slot"},"nonce-1") is False
    assert _runtime_health_matches(slot,1234,{**good,"runtime_launch_nonce":"other"},"nonce-1") is False
