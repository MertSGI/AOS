from pathlib import Path

from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_store import RuntimeStore
from aos import runtime_worker


def _command(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text('{"project_id":"lari"}', encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = ProjectProfile(
        project_id="lari",
        descriptor_path=str(descriptor),
        workspace=str(workspace),
        routing_policy_path=str(policy),
        standing_authority=True,
    )
    return ContinueProjectCommand.from_mapping({"goal": "continue", "continuous": True}, project=profile)


def test_worker_reconciles_once_then_reenters_planning_kernel(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path)
    store.create_command(command.to_dict())
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})

    calls = {"planning": 0, "repair": 0}

    def fake_planning(**kwargs):
        calls["planning"] += 1
        if calls["planning"] == 1:
            return {"disposition": "HUMAN_REQUIRED", "reason": "CANONICAL_CONTRADICTION", "completed_batch_count": 0}
        return {
            "disposition": "PROJECT_COMPLETE",
            "reason": "done",
            "completed_batch_count": 1,
            "canonical_source_sha": "c" * 40,
            "canonical_execution_base_sha": "d" * 40,
        }

    def fake_repair(**kwargs):
        calls["repair"] += 1
        (kwargs["runtime_dir"] / "canonical-repair.json").write_text(
            '{"status":"APPLIED","control_sha_after":"' + "e" * 40 + '","accepted_execution_base_sha":"' + "d" * 40 + '"}',
            encoding="utf-8",
        )
        return {
            "status": "APPLIED",
            "reason": "MISSING_EXECUTION_BASE_BOUND_FROM_ACCEPTED_EVIDENCE",
            "control_sha_after": "e" * 40,
            "accepted_execution_base_sha": "d" * 40,
            "push_mode": "FAST_FORWARD_NO_FORCE",
        }

    monkeypatch.setattr(runtime_worker, "run_autonomous_project", fake_planning)
    monkeypatch.setattr(runtime_worker, "reconcile_missing_execution_base", fake_repair)

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "PROJECT_COMPLETE"
    assert calls == {"planning": 2, "repair": 1}
    assert any(e["event_type"] == "canonical.reconciliation_result" for e in store.read_events(command.command_id))
