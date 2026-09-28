import json
from pathlib import Path

import pytest

from aos.action_center import (
    ACTION_CLASS_HUMAN_DECISION_REQUIRED,
    ActionCenterEngine,
    HumanActionItem,
    validate_and_process_control_request,
)
from aos.controller_relay import ControllerRelayPublisher
from aos.integrity_reconciler import IntegrityReconciler, OUTCOME_BUCKETS
from aos.runtime_store import RuntimeStore, atomic_json
from extensions.autonomy_fabric.execution_backend import (
    EvidenceClass,
    ExecutionCapability,
    ExecutionRequest,
    ExecutionResult,
)


BASE_SHA = "5de4adb1b840f28a9c74a23054e2a8bda8e86a08"


def _request(workspace: Path, task_id: str, scope: list[str]) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        project_id="aos",
        workspace=str(workspace),
        operation_class="FILE_WRITE",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="AOS-E2E-RECONCILIATION",
        write_scope=scope,
        expected_artifacts=["src/result.txt"],
        payload={
            "action": "write_file",
            "path": "src/result.txt",
            "content": "accepted\n",
            "source_sha": BASE_SHA,
        },
    )


def test_e2e_14_cross_lane_collision_prevented_before_mutation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reconciler = IntegrityReconciler(tmp_path / "state" / "integrity")
    first = reconciler.begin(
        _request(workspace, "task-a", ["src"]),
        command_lineage="continue-a",
    )
    second = reconciler.begin(
        _request(workspace, "task-b", ["src/result.txt"]),
        command_lineage="continue-b",
    )

    assert first.disposition == "CLAIMED"
    assert second.disposition == "HOLD"
    assert second.reason.startswith("CROSS_LANE_WRITE_SCOPE_COLLISION:")
    assert not (workspace / "src" / "result.txt").exists()


def test_e2e_15_and_16_dedupe_then_detect_lost_accepted_work(tmp_path):
    workspace = tmp_path / "workspace"
    artifact = workspace / "src" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("accepted\n", encoding="utf-8")
    checkpoint = tmp_path / "coordinator-checkpoint.json"
    checkpoint.write_text("{}\n", encoding="utf-8")
    reconciler = IntegrityReconciler(tmp_path / "state" / "integrity")
    request = _request(workspace, "task-a", ["src"])
    claim = reconciler.begin(request, command_lineage="continue-a")
    result = ExecutionResult(
        backend_id="native_file",
        worker_id="native",
        task_id=request.task_id,
        request_id=request.request_id,
        status="SUCCESS",
        exit_code=0,
        workspace=str(workspace),
        changed_paths=["src/result.txt"],
        artifact_hashes={"src/result.txt": "a" * 64},
        evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
    )
    reconciler.accept(
        claim,
        request,
        result,
        command_lineage="continue-a",
        checkpoint_path=str(checkpoint),
    )

    duplicate = reconciler.begin(request, command_lineage="continue-a-restarted")
    assert duplicate.disposition == "ALREADY_ACCEPTED"
    report = reconciler.reconcile()
    assert report.duplicate_completed_work == 0
    assert report.lost_accepted_work == 0
    assert report.outcome_counts["SUCCESS"] == 1
    assert report.total_executed == sum(report.outcome_counts.values())
    assert report.outcome_partition_valid is True

    artifact.unlink()
    lost = reconciler.reconcile()
    assert lost.lost_accepted_work == 1
    assert any(finding["type"] == "LOST_ACCEPTED_WORK" for finding in lost.findings)


def test_e2e_18_rejects_double_outcome_classification(tmp_path):
    reconciler = IntegrityReconciler(tmp_path / "integrity")
    reconciler.record_execution("execution-a", task_signature="sig-a", task_id="task-a")
    reconciler.record_outcome(
        "SUCCESS", execution_id="execution-a", task_signature="sig-a", task_id="task-a"
    )
    reconciler.record_outcome(
        "TERMINAL_FAILURE",
        execution_id="execution-a",
        task_signature="sig-a",
        task_id="task-a",
    )

    report = reconciler.reconcile()
    assert report.total_executed == 1
    assert report.outcome_partition_valid is False
    assert any(
        finding["type"] == "DOUBLE_OUTCOME_CLASSIFICATION"
        for finding in report.findings
    )


def test_e2e_18_rejects_known_execution_without_outcome(tmp_path):
    reconciler = IntegrityReconciler(tmp_path / "integrity")
    reconciler.record_execution("execution-b", task_signature="sig-b", task_id="task-b")

    report = reconciler.reconcile()
    assert report.total_executed == 1
    assert report.outcome_partition_valid is False
    assert any(
        finding["type"] == "MISSING_OUTCOME_CLASSIFICATION"
        for finding in report.findings
    )


def test_e2e_18_deduplicates_exact_outcome_replay(tmp_path):
    reconciler = IntegrityReconciler(tmp_path / "integrity")
    reconciler.record_execution("execution-c", task_signature="sig-c", task_id="task-c")
    for _ in range(2):
        reconciler.record_outcome(
            "SUCCESS",
            execution_id="execution-c",
            task_signature="sig-c",
            task_id="task-c",
        )

    report = reconciler.reconcile()
    assert report.total_executed == 1
    assert report.outcome_counts["SUCCESS"] == 1
    assert report.outcome_partition_valid is True


def test_e2e_18_six_unique_outcomes_project_to_relay(tmp_path):
    runtime_root = tmp_path / "state"
    reconciler = IntegrityReconciler(runtime_root / "integrity")
    for index, bucket in enumerate(OUTCOME_BUCKETS):
        execution_id = f"execution-{index}"
        reconciler.record_execution(
            execution_id,
            task_signature=f"signature-{index}",
            task_id=f"task-{index}",
        )
        reconciler.record_outcome(
            bucket,
            execution_id=execution_id,
            task_signature=f"signature-{index}",
            task_id=f"task-{index}",
        )

    publisher = ControllerRelayPublisher(
        tmp_path / "relay",
        {"runtime_root": str(runtime_root)},
    )
    snapshot = publisher.collect_snapshot()

    assert snapshot.execution_outcomes == {bucket: 1 for bucket in OUTCOME_BUCKETS}
    assert snapshot.total_executed == len(OUTCOME_BUCKETS)
    assert snapshot.outcome_partition_valid is True
    rendered = publisher.render_markdown(snapshot)
    assert f"TOTAL_EXECUTED={len(OUTCOME_BUCKETS)}" in rendered
    assert "OUTCOME_PARTITION_VALID=YES" in rendered


def test_e2e_23_protected_human_required_survives_running_lane(tmp_path):
    commands = tmp_path / "state" / "commands"
    lanes = [
        ("continue-8a922a56b955ce3ae073fa86", "aos-maintenance", "RUNNING", 98),
        ("continue-b181ddc574c25c2aa0f2a6b9", "lari", "HUMAN_REQUIRED", 528),
        ("continue-61be4ab1af53cfa646d773ce", "lari-ui-v2", "WAITING_FOR_REASONING_PROVIDER", 323),
    ]
    for command_id, project_id, state, batches in lanes:
        command_dir = commands / command_id
        command_dir.mkdir(parents=True)
        (command_dir / "command.json").write_text(
            json.dumps({"project": {"project_id": project_id}}), encoding="utf-8"
        )
        (command_dir / "state.json").write_text(
            json.dumps({"state": state, "completed_batch_count": batches, "attempts": 1}),
            encoding="utf-8",
        )

    publisher = ControllerRelayPublisher(
        tmp_path / "relay",
        {"runtime_root": str(tmp_path / "state")},
    )
    snapshot = publisher.collect_snapshot()
    assert snapshot.human_required is True
    assert "HUMAN_REQUIRED=YES" in publisher.render_markdown(snapshot)


def test_e2e_23_generic_current_human_required_is_aggregated(tmp_path):
    commands = tmp_path / "state" / "commands"
    for command_id, project_id, state in (
        ("continue-project-a", "project-a", "RUNNING"),
        ("continue-project-b", "project-b", "HUMAN_REQUIRED"),
    ):
        command_dir = commands / command_id
        command_dir.mkdir(parents=True)
        (command_dir / "command.json").write_text(
            json.dumps({"project": {"project_id": project_id}}), encoding="utf-8"
        )
        (command_dir / "state.json").write_text(
            json.dumps({"state": state, "lineage_status": "CURRENT"}), encoding="utf-8"
        )

    snapshot = ControllerRelayPublisher(
        tmp_path / "relay", {"runtime_root": str(tmp_path / "state")}
    ).collect_snapshot()
    assert snapshot.human_required is True


def test_e2e_23_historical_human_required_is_excluded(tmp_path):
    commands = tmp_path / "state" / "commands"
    lanes = (
        ("continue-project-a", "project-a", "RUNNING", "CURRENT"),
        ("continue-project-b-old", "project-b", "HUMAN_REQUIRED", "HISTORICAL"),
    )
    for command_id, project_id, state, lineage_status in lanes:
        command_dir = commands / command_id
        command_dir.mkdir(parents=True)
        (command_dir / "command.json").write_text(
            json.dumps({"project": {"project_id": project_id}}), encoding="utf-8"
        )
        (command_dir / "state.json").write_text(
            json.dumps({"state": state, "lineage_status": lineage_status}), encoding="utf-8"
        )

    snapshot = ControllerRelayPublisher(
        tmp_path / "relay", {"runtime_root": str(tmp_path / "state")}
    ).collect_snapshot()
    assert snapshot.human_required is False


def test_e2e_23_superseding_command_excludes_old_human_required(tmp_path):
    commands = tmp_path / "state" / "commands"
    old_id = "continue-project-b-old"
    current_id = "continue-project-b-current"
    old_dir = commands / old_id
    old_dir.mkdir(parents=True)
    (old_dir / "command.json").write_text(
        json.dumps({"project": {"project_id": "project-b"}}), encoding="utf-8"
    )
    (old_dir / "state.json").write_text(
        json.dumps({
            "state": "HUMAN_REQUIRED",
            "lineage_status": "SUPERSEDED",
            "superseded_by_command_id": current_id,
        }),
        encoding="utf-8",
    )
    current_dir = commands / current_id
    current_dir.mkdir(parents=True)
    (current_dir / "command.json").write_text(
        json.dumps({"project": {"project_id": "project-b"}}), encoding="utf-8"
    )
    (current_dir / "state.json").write_text(
        json.dumps({"state": "RUNNING", "lineage_status": "CURRENT"}), encoding="utf-8"
    )

    snapshot = ControllerRelayPublisher(
        tmp_path / "relay", {"runtime_root": str(tmp_path / "state")}
    ).collect_snapshot()
    by_id = {lane["command_id"]: lane for lane in snapshot.lanes}
    assert snapshot.human_required is False
    assert by_id[old_id]["lineage_status"] == "SUPERSEDED"
    assert by_id[current_id]["lineage_status"] == "CURRENT"


def test_e2e_10_to_12_action_freshness_and_provenance(tmp_path):
    store = RuntimeStore(tmp_path / "state")
    engine = ActionCenterEngine(tmp_path / "actions")
    command_id = "continue-b181ddc574c25c2aa0f2a6b9"
    store.create_command({
        "command_id": command_id,
        "project": {"project_id": "lari"},
        "goal": "Preserve the protected lineage",
    })
    store.write_state(
        command_id,
        state="HUMAN_REQUIRED",
        disposition="HUMAN_DECISION_REQUIRED",
        completed_batch_count=528,
        strategy_generation=4,
        canonical_source_sha=BASE_SHA,
    )
    checkpoint_path = store.command_dir(command_id) / "project-runtime" / "planning-kernel-checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    atomic_json(checkpoint_path, {
        "batch_number": 528,
        "strategy_generation": 4,
        "canonical_source_sha": BASE_SHA,
    })
    action_id = "act-current-lari"
    engine.create_or_update_action(HumanActionItem(
        action_id=action_id,
        project="lari",
        command_id=command_id,
        current_batch=528,
        created_batch=528,
        created_generation=4,
        action_class=ACTION_CLASS_HUMAN_DECISION_REQUIRED,
        risk_class="MEDIUM",
        why_stopped="Authority decision",
        expected_state="HUMAN_REQUIRED",
        expected_lane_state="HUMAN_REQUIRED",
        observed_state="HUMAN_REQUIRED",
        exact_blocker="DECISION_REQUIRED",
        why_automation_cannot_continue="Human authority is required",
        evidence_references=[],
        canonical_revision=BASE_SHA,
        workspace_fingerprint="fp",
        decision_required="Continue?",
        available_options=[],
        option_consequences={},
        reversibility="REVERSIBLE",
        safe_default_if_any=None,
        authority_boundary="OWNER",
        created_at="2026-09-28T00:00:00Z",
    ))
    payload = {
        "request_id": "req-e2e-action-freshness",
        "schema_version": "0.1.0",
        "project_id": "lari",
        "actor_type": "human_owner",
        "request_type": "RESUME",
        "base_control_sha": BASE_SHA,
        "requested_change": "Resume exact protected lineage",
        "reason": "Fresh human decision",
        "requested_at": "2026-09-28T00:00:00Z",
        "extensions": {"action_id": action_id, "command_id": command_id},
    }

    with pytest.raises(PermissionError, match="provenance must be PROVEN"):
        validate_and_process_control_request(payload, engine, store, BASE_SHA)

    accepted = validate_and_process_control_request(
        payload,
        engine,
        store,
        BASE_SHA,
        runtime_provenance="PROVEN",
    )
    assert accepted["status"] == "ACCEPTED"
    state = store.read_state(command_id)
    assert state["state"] == "QUEUED"
    assert state["completed_batch_count"] == 528


def test_e2e_17_lower_priority_writer_cannot_replace_supervisor_snapshot(tmp_path):
    relay_dir = tmp_path / "relay"
    config = {"runtime_root": str(tmp_path / "state")}
    supervisor = ControllerRelayPublisher(
        relay_dir, config, writer_instance_id="aos-supervisor-100"
    )
    supervisor.publish_local(supervisor.collect_snapshot())

    dev_writer = ControllerRelayPublisher(
        relay_dir, config, writer_instance_id="pytest-dev-writer"
    )
    dev_writer.publish_local(dev_writer.collect_snapshot())

    latest = json.loads((relay_dir / "LATEST.json").read_text(encoding="utf-8"))
    assert latest["writer_instance_id"] == "aos-supervisor-100"
