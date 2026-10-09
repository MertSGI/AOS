"""Tests for KCP verified-delivery receipt integration in AOS Runtime V1."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from aos import runtime_worker
from aos.knowledge.accepted_work import accepted_work_coverage
from aos.knowledge.hooks import ledger_for_runtime, resolve_knowledge_ledger
from aos.knowledge.model import KnowledgeEventType
from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_store import RuntimeStore


def _command(tmp_path: Path, project_id: str = "lari") -> ContinueProjectCommand:
    profile = ProjectProfile(
        project_id=project_id,
        descriptor_path=str(tmp_path / f"descriptor-{project_id}.json"),
        workspace=str(tmp_path / f"workspace-{project_id}"),
        routing_policy_path=str(tmp_path / f"policy-{project_id}.json"),
    )
    (tmp_path / f"descriptor-{project_id}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"policy-{project_id}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"workspace-{project_id}").mkdir(exist_ok=True)
    return ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)


def test_verified_worker_outcome_creates_valid_kcp_receipt(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "a" * 40
    result_sha = "b" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "all tasks completed and verified",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "completed_task_ids": ["task-1", "task-2"],
            "canonical_source_sha": result_sha,
            "canonical_execution_base_sha": base_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"

    ledger = ledger_for_runtime(runtime_home)
    events = ledger.read_events()
    assert len(events) >= 1

    verification_events = [
        e for e in events
        if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value
    ]
    assert len(verification_events) == 1
    receipt = verification_events[0]
    assert receipt["project_id"] == "lari"
    assert receipt["result_sha"] == result_sha
    assert receipt["base_sha"] == base_sha
    assert receipt["ci"]["status"] == "PASS"
    assert receipt["claims"]["verified_worker_execution"] is True
    assert receipt["production"] == "NO_GO"
    assert receipt["paid_fallback"] == "DISABLED"


def test_generated_run_plan_without_execution_does_not_create_receipt(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    # Simulates only a plan generated, but zero completed tasks or batches executed
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "HUMAN_REQUIRED",
            "reason": "plan requires confirmation",
            "completed_batch_count": 0,
            "completed_task_ids": [],
            "failed_task_ids": [],
            "canonical_source_sha": "a" * 40,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "HUMAN_REQUIRED"

    ledger = ledger_for_runtime(runtime_home)
    assert len(ledger.read_events()) == 0


def test_retry_recovery_is_idempotent(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "c" * 40
    result_sha = "d" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    outcome = {
        "disposition": "PROJECT_COMPLETE",
        "reason": "verified execution",
        "completed_batch_count": 1,
        "completed_batches": [{"batch_number": 1}],
        "completed_task_ids": ["task-1"],
        "canonical_source_sha": result_sha,
        "canonical_execution_base_sha": base_sha,
    }
    monkeypatch.setattr(runtime_worker, "run_autonomous_project", lambda **kwargs: outcome)

    # First execution
    runtime_worker.execute_command(root, command.command_id)
    ledger = ledger_for_runtime(runtime_home)
    events_first = ledger.read_events()
    assert len(events_first) == 1

    # Second execution (retry/recovery replay of same command and verified batch)
    runtime_worker.execute_command(root, command.command_id)
    events_second = ledger.read_events()
    assert len(events_second) == 1
    assert events_second[0]["event_id"] == events_first[0]["event_id"]


def test_read_only_verification_does_not_fabricate_implementation_receipt(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    result_sha = "e" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "read-only verification pass",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "completed_task_ids": ["read-verification-check"],
            "canonical_source_sha": result_sha,
        },
    )

    runtime_worker.execute_command(root, command.command_id)
    ledger = ledger_for_runtime(runtime_home)
    event_types = [e["event_type"] for e in ledger.read_events()]

    assert KnowledgeEventType.VERIFICATION_RECEIPT.value in event_types
    assert KnowledgeEventType.IMPLEMENTATION_RECEIPT.value not in event_types


def test_ledger_write_failure_cannot_produce_false_acceptance_or_promotion(tmp_path: Path, monkeypatch):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    result_sha = "f" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "done",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "completed_task_ids": ["task-1"],
            "canonical_source_sha": result_sha,
        },
    )

    # Force ledger append failure
    ledger = ledger_for_runtime(runtime_home)
    monkeypatch.setattr(ledger, "append", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(runtime_worker, "resolve_knowledge_ledger", lambda **kwargs: ledger)

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"
    assert "kcp_verification_receipt" not in result["receipt"]

    coverage = accepted_work_coverage(ledger, project_id="lari", result_sha=result_sha)
    assert coverage["covered"] is False
    assert coverage["status"] == "MISSING_ACCEPTED_WORK_COVERAGE"


@pytest.mark.parametrize("project_id", ["lari", "lari-ui-v2"])
def test_project_identities_preserved(tmp_path: Path, monkeypatch, project_id: str):
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, project_id)
    store.create_command(command.to_dict())

    result_sha = "1" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "completed",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "completed_task_ids": ["task-1"],
            "canonical_source_sha": result_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"

    ledger = ledger_for_runtime(runtime_home)
    events = [e for e in ledger.read_events() if e["project_id"] == project_id]
    assert len(events) == 1
    assert events[0]["project_id"] == project_id


def test_existing_recovery_pause_behavior_preserved(tmp_path: Path, monkeypatch):
    from aos.runtime_maintenance import persist_maintenance

    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    profile = ProjectProfile(
        project_id="lari",
        descriptor_path=str(tmp_path / "descriptor.json"),
        workspace=str(tmp_path / "workspace"),
        routing_policy_path=str(tmp_path / "policy.json"),
    )
    (tmp_path / "descriptor.json").write_text("{}", encoding="utf-8")
    (tmp_path / "policy.json").write_text("{}", encoding="utf-8")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    command = ContinueProjectCommand.from_mapping(
        {"goal": "continue", "continuous": True},
        project=profile,
    )
    store.create_command(command.to_dict())

    def fake_run(**kwargs):
        persist_maintenance(root, paused=True, reason="maintenance_pause")
        return {
            "disposition": "BOUNDED_RUN_EXHAUSTED",
            "reason": "cycle ended",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "completed_task_ids": ["task-1"],
            "canonical_source_sha": "2" * 40,
            "canonical_execution_base_sha": "3" * 40,
        }

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(runtime_worker, "run_autonomous_project", fake_run)

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "RUNNING"
    assert result["disposition"] == "PAUSED_SAFE"
    assert result["receipt"]["pause_safe"] is True
