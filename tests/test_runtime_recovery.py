from pathlib import Path
import json

from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_server import RuntimeEngine


def _config(tmp_path: Path):
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def test_unfinished_command_is_respawned_without_new_user_command(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        profile = ProjectProfile(
            project_id="lari",
            descriptor_path=str(tmp_path / "descriptor.json"),
            workspace=str(tmp_path / "workspace"),
            routing_policy_path=str(tmp_path / "policy.json"),
        )
        command = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)
        engine.store.create_command(command.to_dict())
        engine.store.write_state(command.command_id, state="RUNNING", worker_pid=99999999, attempts=1)
        calls = []
        monkeypatch.setattr("aos.runtime_server.pid_alive", lambda pid: False)
        monkeypatch.setattr(engine, "_spawn_worker", lambda command_id, recovered: calls.append((command_id, recovered)) or 123)
        engine._recover_one(command.command_id)
        assert calls == [(command.command_id, True)]
    finally:
        engine.shutdown()


def test_waiting_command_retry_backoff_and_wake_recovery(tmp_path, monkeypatch):
    import time
    engine = RuntimeEngine(_config(tmp_path))
    try:
        profile = ProjectProfile(
            project_id="lari",
            descriptor_path=str(tmp_path / "descriptor.json"),
            workspace=str(tmp_path / "workspace"),
            routing_policy_path=str(tmp_path / "policy.json"),
        )
        command = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)
        engine.store.create_command(command.to_dict())

        # When retry_after_epoch is in the future, worker should NOT be spawned
        future_epoch = time.time() + 1000.0
        engine.store.write_state(
            command.command_id,
            state="WAITING_FOR_REASONING_PROVIDER",
            disposition="WAITING_FOR_REASONING_PROVIDER",
            worker_pid=None,
            retry_after_epoch=future_epoch,
            attempts=1,
        )
        calls = []
        monkeypatch.setattr(engine, "_spawn_worker", lambda command_id, recovered: calls.append((command_id, recovered)) or 456)
        engine._recover_one(command.command_id)
        assert calls == []

        # When retry_after_epoch has passed, worker should be respawned with recovered=True
        past_epoch = time.time() - 10.0
        engine.store.write_state(
            command.command_id,
            retry_after_epoch=past_epoch,
        )
        engine._recover_one(command.command_id)
        assert calls == [(command.command_id, True)]
    finally:
        engine.shutdown()


def test_same_fingerprint_recovery_escalates_once_then_holds(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        profile = ProjectProfile(
            project_id="lari",
            descriptor_path=str(tmp_path / "descriptor.json"),
            workspace=str(tmp_path / "workspace"),
            routing_policy_path=str(tmp_path / "policy.json"),
        )
        command = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)
        engine.store.create_command(command.to_dict())
        engine.store.write_state(
            command.command_id,
            state="RUNNING",
            worker_pid=99999999,
            attempts=1,
            failure_class="PLANNER_VALIDATION",
            canonical_source_sha="a" * 40,
            canonical_execution_base_sha="b" * 40,
        )
        project_runtime = engine.store.command_dir(command.command_id) / "project-runtime"
        project_runtime.mkdir(parents=True)
        (project_runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
            "phase": "BOUNDED_RUN_EXHAUSTED",
            "batch_number": 4,
            "completed_batch_count": 3,
            "canonical_source_sha": "a" * 40,
            "canonical_execution_base_sha": "b" * 40,
            "objective_id": "objective-1",
        }), encoding="utf-8")
        calls = []
        monkeypatch.setattr("aos.runtime_server.pid_alive", lambda _pid: False)
        monkeypatch.setattr(
            engine, "_spawn_worker",
            lambda command_id, recovered: calls.append((command_id, recovered)) or 123,
        )

        engine._recover_one(command.command_id)
        engine._recover_one(command.command_id)
        engine._recover_one(command.command_id)
        engine._recover_one(command.command_id)

        state = engine.store.read_state(command.command_id)
        assert len(calls) == 2
        assert state["state"] == "HUMAN_REQUIRED"
        assert state["failure_class"] == "RECOVERY_CHURN_GUARD"
        assert state["same_fingerprint_respawns"] == 3
        assert state["strategy_generation"] == 1
        assert state["retry_after_epoch"] == 0
    finally:
        engine.shutdown()


def test_recovery_progress_or_failure_family_change_resets_streak(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        profile = ProjectProfile(
            project_id="lari",
            descriptor_path=str(tmp_path / "descriptor.json"),
            workspace=str(tmp_path / "workspace"),
            routing_policy_path=str(tmp_path / "policy.json"),
        )
        command = ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)
        engine.store.create_command(command.to_dict())
        engine.store.write_state(command.command_id, state="RUNNING", worker_pid=None, failure_class="TIMEOUT")
        project_runtime = engine.store.command_dir(command.command_id) / "project-runtime"
        project_runtime.mkdir(parents=True)
        checkpoint_path = project_runtime / "planning-kernel-checkpoint.json"
        checkpoint_path.write_text(json.dumps({"batch_number": 1, "completed_batch_count": 0}), encoding="utf-8")
        calls = []
        monkeypatch.setattr("aos.runtime_server.pid_alive", lambda _pid: False)
        monkeypatch.setattr(engine, "_spawn_worker", lambda command_id, recovered: calls.append(command_id) or 123)

        engine._recover_one(command.command_id)
        checkpoint_path.write_text(json.dumps({"batch_number": 2, "completed_batch_count": 1}), encoding="utf-8")
        engine._recover_one(command.command_id)
        state_after_progress = engine.store.read_state(command.command_id)
        assert state_after_progress["same_fingerprint_respawns"] == 1

        engine.store.write_state(command.command_id, failure_class="CONTRACT_FAILURE")
        engine._recover_one(command.command_id)
        state_after_family = engine.store.read_state(command.command_id)
        assert state_after_family["same_fingerprint_respawns"] == 1
        assert len(calls) == 3
    finally:
        engine.shutdown()
