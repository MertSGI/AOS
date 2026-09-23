import json

import pytest

from aos.provider_observation import ObservationSource, RateLimitObservation
from aos.quota_governor import QuotaGovernor
from aos.resource_ledger import (
    ResourceEventType,
    ResourceLedger,
    ResourceLedgerCorruptionError,
)
from aos.runtime_store import RuntimeStore


def _rate_observation(retry_at: float = 1200.0) -> RateLimitObservation:
    return RateLimitObservation(
        provider_id="nemotron",
        model_id="nemotron-model",
        task_class="structured_planning",
        observed_at="2026-09-24T00:00:00+00:00",
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=retry_at,
        request_limit=30,
        request_remaining=0,
        quota_scope="MODEL",
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
        field_sources={
            "retry_at_epoch": ObservationSource.PROVIDER_METADATA.value,
            "request_limit": ObservationSource.PROVIDER_METADATA.value,
            "request_remaining": ObservationSource.PROVIDER_METADATA.value,
        },
    )


def test_append_replay_and_summary_are_restart_stable(tmp_path):
    path = tmp_path / "resource-ledger.jsonl"
    ledger = ResourceLedger(path, clock=lambda: 1000.0)
    ledger.append(
        ResourceEventType.ATTEMPT_STARTED,
        idempotency_key="attempt:abc:started",
        payload={"attempt_id": "abc", "provider_id": "nemotron", "status": "STARTED"},
    )
    ledger.append(
        ResourceEventType.RESOURCE_USAGE,
        idempotency_key="attempt:abc:usage",
        payload={
            "attempt_id": "abc",
            "provider_id": "nemotron",
            "request_count": 1,
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "cost_actual_usd": 0,
            "raw_prompt": "must never persist",
        },
    )
    ledger.append(
        ResourceEventType.ATTEMPT_FINISHED,
        idempotency_key="attempt:abc:finished",
        payload={"attempt_id": "abc", "provider_id": "nemotron", "status": "SUCCESS"},
    )

    first = ledger.summary()
    restarted = ResourceLedger(path, clock=lambda: 2000.0)

    assert restarted.summary() == first
    assert first["event_count"] == 3
    assert first["attempts_started"] == 1
    assert first["attempts_finished"] == 1
    assert first["request_count"] == 1
    assert first["total_tokens"] == 14
    assert "must never persist" not in path.read_text(encoding="utf-8")
    assert "raw_prompt" not in path.read_text(encoding="utf-8")


def test_idempotency_prevents_double_accounting_and_rejects_collision(tmp_path):
    ledger = ResourceLedger(tmp_path / "resource-ledger.jsonl", clock=lambda: 1000.0)
    payload = {"attempt_id": "abc", "request_count": 1, "total_tokens": 7}
    first = ledger.append(
        ResourceEventType.RESOURCE_USAGE,
        idempotency_key="attempt:abc:usage",
        payload=payload,
    )
    duplicate = ledger.append(
        ResourceEventType.RESOURCE_USAGE,
        idempotency_key="attempt:abc:usage",
        payload=payload,
    )

    assert duplicate.event_id == first.event_id
    assert ledger.summary()["event_count"] == 1
    assert ledger.summary()["total_tokens"] == 7
    with pytest.raises(ValueError, match="conflicts"):
        ledger.append(
            ResourceEventType.RESOURCE_USAGE,
            idempotency_key="attempt:abc:usage",
            payload={**payload, "total_tokens": 8},
        )


def test_incomplete_tail_is_ignored_then_repaired_on_append(tmp_path):
    path = tmp_path / "resource-ledger.jsonl"
    ledger = ResourceLedger(path, clock=lambda: 1000.0)
    ledger.append(
        ResourceEventType.ATTEMPT_STARTED,
        idempotency_key="attempt:abc:started",
        payload={"attempt_id": "abc", "status": "STARTED"},
    )
    with path.open("ab") as handle:
        handle.write(b'{"incomplete":')

    recovered = ResourceLedger(path, clock=lambda: 1001.0)
    assert recovered.corrupt is False
    assert recovered.summary()["truncated_tail_ignored"] is True
    recovered.append(
        ResourceEventType.ATTEMPT_FINISHED,
        idempotency_key="attempt:abc:finished",
        payload={"attempt_id": "abc", "status": "SUCCESS"},
    )

    assert ResourceLedger(path).summary()["event_count"] == 2
    assert "incomplete" not in path.read_text(encoding="utf-8")


def test_non_tail_corruption_fails_closed(tmp_path):
    path = tmp_path / "resource-ledger.jsonl"
    path.write_text('{"broken":true}\n{"also":"broken"}\n', encoding="utf-8")

    ledger = ResourceLedger(path)

    assert ledger.corrupt is True
    assert ledger.summary()["fail_closed"] is True
    with pytest.raises(ResourceLedgerCorruptionError):
        ledger.append(
            ResourceEventType.ATTEMPT_STARTED,
            idempotency_key="attempt:abc:started",
            payload={"attempt_id": "abc", "status": "STARTED"},
        )


def test_quota_snapshot_rebuilds_from_ledger(tmp_path):
    ledger = ResourceLedger(tmp_path / "resource-ledger.jsonl", clock=lambda: 1000.0)
    snapshot = tmp_path / "quota-governor.json"
    governor = QuotaGovernor(snapshot, clock=lambda: 1000.0, ledger=ledger)
    governor.record(_rate_observation())
    assert governor.decision(
        "nemotron", "nemotron-model", "structured_planning"
    ).retry_at_epoch == 1200.0

    snapshot.write_text("{corrupt", encoding="utf-8")
    rebuilt = QuotaGovernor(snapshot, clock=lambda: 1050.0, ledger=ResourceLedger(ledger.log_path))
    decision = rebuilt.decision("nemotron", "nemotron-model", "structured_planning")

    assert decision.eligible is False
    assert decision.retry_at_epoch == 1200.0
    persisted = json.loads(snapshot.read_text(encoding="utf-8"))
    assert persisted["ledger_rate_head_hash"] != "0" * 64


def test_corrupt_resource_state_does_not_mutate_completed_project_state(tmp_path):
    store = RuntimeStore(tmp_path / "runtime")
    command_id = "continue-project-state"
    store.create_command({
        "command_id": command_id,
        "project": {"project_id": "project-a"},
        "goal": "retain completed work",
    })
    before = store.write_state(
        command_id,
        state="PROJECT_COMPLETE",
        disposition="PROJECT_COMPLETE",
        completed_batch_count=3,
    )
    ledger_path = store.resource_os_dir(command_id) / "resource-ledger.jsonl"
    ledger_path.write_text('{"broken":true}\n{"also":"broken"}\n', encoding="utf-8")

    ledger = ResourceLedger(ledger_path)
    governor = QuotaGovernor(
        store.resource_os_dir(command_id) / "quota-governor.json",
        ledger=ledger,
    )
    decision = governor.decision("nemotron", "model", "structured_planning")
    after = store.read_state(command_id)

    assert ledger.corrupt is True
    assert decision.eligible is False
    assert decision.reason == "CORRUPT_QUOTA_STATE_FAIL_CLOSED"
    assert after["state"] == before["state"] == "PROJECT_COMPLETE"
    assert after["completed_batch_count"] == before["completed_batch_count"] == 3
