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


def test_1_completed_batch_without_test_build_proof_produces_no_pass(tmp_path: Path, monkeypatch):
    """1. Completed batch without TEST/BUILD proof -> no PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "a" * 40
    candidate_sha = "b" * 40
    control_sha = "c" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    # Completed worker execution without TEST/BUILD verification proof
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "tasks completed without verification proof",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1", "task-2"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                    },
                }
            ],
            "candidate_sha": candidate_sha,
            "canonical_source_sha": control_sha,
            "canonical_execution_base_sha": base_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"
    assert "kcp_verification_receipt" not in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "NOT_VERIFIED"

    ledger = ledger_for_runtime(root.parent)
    assert len(ledger.read_events()) == 0


def test_2_bounded_run_exhausted_with_completed_tasks_produces_no_automatic_pass(tmp_path: Path, monkeypatch):
    """2. BOUNDED_RUN_EXHAUSTED with completed tasks -> no automatic PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "BOUNDED_RUN_EXHAUSTED",
            "reason": "cycle ended",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"test_suite": "passed"},
                    },
                }
            ],
            "candidate_sha": "a" * 40,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "NOT_VERIFIED"

    ledger = ledger_for_runtime(root.parent)
    assert len(ledger.read_events()) == 0


def test_3_missing_progress_produces_no_pass(tmp_path: Path, monkeypatch):
    """3. Missing progress -> no PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "batch without explicit progress field",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        # Missing "progress" field
                        "verification_evidence": {"test": "ok"},
                    },
                }
            ],
            "candidate_sha": "a" * 40,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "NOT_VERIFIED"


def test_4_missing_host_execution_artifact_produces_no_pass(tmp_path: Path, monkeypatch):
    """4. Missing host execution artifact -> no PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    # completed_batches points to batch 1, but project-runtime/batches/batch-0001/host-receipt.json does not exist
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "batch claimed without host artifact on disk",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "candidate_sha": "a" * 40,
            "verification_evidence": {"test": "ok"},
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "NOT_VERIFIED"


def test_5_candidate_sha_without_verification_binding_produces_no_pass(tmp_path: Path, monkeypatch):
    """5. Candidate SHA without verification binding -> no PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "completed code tasks without test/build verification",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-code-change"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                    },
                }
            ],
            "candidate_sha": "a" * 40,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "NOT_VERIFIED"


def test_6_real_successful_verification_evidence_with_matching_exact_sha_creates_valid_pass(tmp_path: Path, monkeypatch):
    """6. Real successful verification evidence with matching exact SHA -> valid PASS."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "a" * 40
    candidate_sha = "b" * 40
    control_sha = "c" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "verified execution with test evidence",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-test-runner"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"runner": "pytest", "tests_passed": 12},
                        "verified_run_types": ["TEST"],
                    },
                }
            ],
            "candidate_sha": candidate_sha,
            "canonical_source_sha": control_sha,
            "canonical_execution_base_sha": base_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"
    assert "kcp_verification_receipt" in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "PASS"

    ledger = ledger_for_runtime(runtime_home)
    events = ledger.read_events()
    v_events = [e for e in events if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value]
    assert len(v_events) == 1
    receipt = v_events[0]
    assert receipt["project_id"] == "lari"
    assert receipt["result_sha"] == candidate_sha
    assert receipt["base_sha"] == base_sha
    assert receipt["claims"]["canonical_control_sha"] == control_sha
    assert receipt["ci"]["status"] == "PASS"
    assert receipt["claims"]["verified_worker_execution"] is True
    assert receipt["production"] == "NO_GO"
    assert receipt["paid_fallback"] == "DISABLED"


def test_7_nested_failure_or_failed_batch_count_prevents_aggregate_pass(tmp_path: Path, monkeypatch):
    """7. Any nested failure or failed batch count -> no aggregate PASS."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    # Case A: nested failed task ID
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "batch had failed task",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": ["task-2-failed"],
                        "progress": 100.0,
                        "verification_evidence": {"test": "fail"},
                    },
                }
            ],
            "candidate_sha": "a" * 40,
        },
    )
    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]
    assert store.read_state(command.command_id)["kcp_verification_status"] == "NOT_VERIFIED"

    # Case B: failed_batch_count > 0
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "earlier batch failed",
            "completed_batch_count": 1,
            "failed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 2,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"test": "ok"},
                    },
                }
            ],
            "candidate_sha": "a" * 40,
        },
    )
    result = runtime_worker.execute_command(root, command.command_id)
    assert "kcp_verification_receipt" not in result["receipt"]
    assert store.read_state(command.command_id)["kcp_verification_status"] == "NOT_VERIFIED"


def test_8_control_sha_differs_from_product_sha_uses_verified_product_sha(tmp_path: Path, monkeypatch):
    """Control SHA differs from product SHA: receipt uses verified product SHA."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    product_sha = "1" * 40
    control_sha = "9" * 40
    base_sha = "0" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "completed batch",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-verify"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"build": "ok"},
                    },
                }
            ],
            "candidate_sha": product_sha,
            "canonical_source_sha": control_sha,
            "canonical_execution_base_sha": base_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"

    ledger = ledger_for_runtime(runtime_home)
    receipts = [
        e for e in ledger.read_events()
        if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value
    ]
    assert len(receipts) == 1
    r = receipts[0]
    # Bound to verified product SHA, NOT control SHA
    assert r["result_sha"] == product_sha
    assert r["result_sha"] != control_sha
    # Control SHA preserved as lineage
    assert r["claims"]["canonical_control_sha"] == control_sha
    assert f"control_lineage:{control_sha}" in r["evidence_refs"]


def test_9_ledger_write_failure_produces_explicit_failed_status(tmp_path: Path, monkeypatch):
    """Ledger write failure produces explicit FAILED status and cannot satisfy acceptance."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    product_sha = "f" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "verified execution",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"test": "ok"},
                    },
                }
            ],
            "candidate_sha": product_sha,
        },
    )

    # Force ledger append failure
    ledger = ledger_for_runtime(runtime_home)
    monkeypatch.setattr(ledger, "append", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ledger disk full")))
    monkeypatch.setattr(runtime_worker, "resolve_knowledge_ledger", lambda **kwargs: ledger)

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"
    assert "kcp_verification_receipt" not in result["receipt"]

    # Explicit failure classification recorded
    assert result["receipt"]["kcp_error"]["classification"] == "KCP_RECEIPT_WRITE_FAILED"
    assert "ledger disk full" in result["receipt"]["kcp_error"]["error"]

    # State stores failed verification status
    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "FAILED"
    assert saved_state["kcp_error"] == "KCP_RECEIPT_WRITE_FAILED"

    # Cannot satisfy acceptance
    coverage = accepted_work_coverage(ledger, project_id="lari", result_sha=product_sha)
    assert coverage["covered"] is False
    assert coverage["status"] == "MISSING_ACCEPTED_WORK_COVERAGE"


def test_10_replayed_command_does_not_create_duplicate_receipts(tmp_path: Path, monkeypatch):
    """Replayed command does not create duplicate receipts."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "c" * 40
    candidate_sha = "d" * 40

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    outcome = {
        "disposition": "PROJECT_COMPLETE",
        "reason": "verified execution",
        "completed_batch_count": 1,
        "completed_batches": [
            {
                "batch_number": 1,
                "receipt": {
                    "completed_task_ids": ["task-1"],
                    "failed_task_ids": [],
                    "progress": 100.0,
                    "verification_evidence": {"test": "ok"},
                },
            }
        ],
        "candidate_sha": candidate_sha,
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


def test_11_read_only_task_creates_no_fabricated_implementation_receipt(tmp_path: Path, monkeypatch):
    """Read-only task creates no fabricated implementation receipt."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    base_sha = "e" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "read-only baseline verification pass",
            "read_only_verification": True,
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["read-verification-check"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"baseline_check": "ok"},
                    },
                }
            ],
            "canonical_execution_base_sha": base_sha,
        },
    )

    runtime_worker.execute_command(root, command.command_id)
    ledger = ledger_for_runtime(runtime_home)
    events = ledger.read_events()
    event_types = [e["event_type"] for e in events]

    assert KnowledgeEventType.VERIFICATION_RECEIPT.value in event_types
    assert KnowledgeEventType.IMPLEMENTATION_RECEIPT.value not in event_types
    # Bound to canonical_execution_base_sha
    v_receipt = [e for e in events if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value][0]
    assert v_receipt["result_sha"] == base_sha
    assert v_receipt["claims"]["read_only_verification"] is True


@pytest.mark.parametrize("project_id", ["lari", "lari-ui-v2"])
def test_12_both_lari_and_lari_ui_v2_preserve_their_respective_identities(tmp_path: Path, monkeypatch, project_id: str):
    """Both lari and lari-ui-v2 preserve their respective identities."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, project_id)
    store.create_command(command.to_dict())

    candidate_sha = "1" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "completed",
            "completed_batch_count": 1,
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"test": "ok"},
                    },
                }
            ],
            "candidate_sha": candidate_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"

    ledger = ledger_for_runtime(runtime_home)
    events = [e for e in ledger.read_events() if e["project_id"] == project_id]
    assert len(events) == 1
    assert events[0]["project_id"] == project_id
    assert events[0]["result_sha"] == candidate_sha


def test_13_paused_safe_and_existing_recovery_behavior_remain_intact(tmp_path: Path, monkeypatch):
    """PAUSED_SAFE and existing recovery behavior remain intact."""
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
            "completed_batches": [
                {
                    "batch_number": 1,
                    "receipt": {
                        "completed_task_ids": ["task-1"],
                        "failed_task_ids": [],
                        "progress": 100.0,
                        "verification_evidence": {"test": "ok"},
                    },
                }
            ],
            "candidate_sha": "2" * 40,
            "canonical_execution_base_sha": "3" * 40,
        }

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(runtime_worker, "run_autonomous_project", fake_run)

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "RUNNING"
    assert result["disposition"] == "PAUSED_SAFE"
    assert result["receipt"]["pause_safe"] is True


def test_14_disk_artifact_host_receipt_validation(tmp_path: Path, monkeypatch):
    """Validate completed batch receipts against disk host-receipt.json artifacts."""
    root = tmp_path / "runtime"
    runtime_home = root.parent
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    # Create project runtime batch directory with host-receipt.json
    command_root = store.command_dir(command.command_id)
    batch_dir = command_root / "project-runtime" / "batches" / "batch-0001"
    batch_dir.mkdir(parents=True, exist_ok=True)
    host_receipt = {
        "completed_task_ids": ["task-host-1", "task-host-2"],
        "failed_task_ids": [],
        "progress": 100.0,
        "verification_evidence": {"test": "ok"},
    }
    (batch_dir / "host-receipt.json").write_text(json.dumps(host_receipt), encoding="utf-8")

    candidate_sha = "7" * 40
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "PROJECT_COMPLETE",
            "reason": "batch verified via disk artifact",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1}],
            "candidate_sha": candidate_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"

    ledger = ledger_for_runtime(runtime_home)
    receipts = [
        e for e in ledger.read_events()
        if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value
    ]
    assert len(receipts) == 1
    assert receipts[0]["ci"]["completed_task_count"] == 2
    assert receipts[0]["result_sha"] == candidate_sha
