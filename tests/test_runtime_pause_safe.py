from pathlib import Path

import pytest

import aos.runtime_server as runtime_server
import aos.runtime_worker as runtime_worker
from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_maintenance import PAUSED_SAFE, persist_maintenance, read_maintenance
from aos.runtime_server import RuntimeEngine
from aos.runtime_store import RuntimeStore


def _config(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(
        (repo_root / "descriptors" / "lari.autonomous-host.descriptor.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text(
        (repo_root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
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


def _command(engine: RuntimeEngine, tmp_path: Path, command_id: str = "paused-command"):
    profile = ProjectProfile(
        "lari",
        str(tmp_path / "descriptor.json"),
        str(tmp_path / "workspace"),
        str(tmp_path / "policy.json"),
    )
    command = ContinueProjectCommand.from_mapping(
        {"command_id": command_id, "goal": "continue"},
        project=profile,
    )
    engine.store.create_command(command.to_dict())
    return command


def test_pause_blocks_new_goal_direct_spawn_and_recovery(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        command = _command(engine, tmp_path)
        engine.store.write_state(command.command_id, state="RUNNING", worker_pid=None)
        spawned = []
        monkeypatch.setattr(
            runtime_server,
            "popen_headless",
            lambda *args, **kwargs: spawned.append((args, kwargs)),
        )
        engine.pause_safe()

        assert engine._spawn_worker(command.command_id, recovered=True) is None
        engine._recover_one(command.command_id)
        with pytest.raises(RuntimeError, match="paused-safe"):
            engine.submit_continue({"goal": "must remain held"})
        assert spawned == []
        assert engine.store.list_command_ids() == [command.command_id]
        health = engine.health()
        assert health["paused"] is True
        assert health["autonomous_spawning_enabled"] is False
    finally:
        engine.shutdown()


def test_pause_preserves_existing_worker_and_blocks_restart(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        command = _command(engine, tmp_path)
        engine.store.write_state(command.command_id, state="RUNNING", worker_pid=424242)
        killed = []
        spawned = []
        monkeypatch.setattr(runtime_server, "pid_alive", lambda pid: True)
        monkeypatch.setattr(runtime_server, "terminate_process_tree", lambda *args, **kwargs: killed.append(args))
        monkeypatch.setattr(engine, "_spawn_worker", lambda *args, **kwargs: spawned.append(args))

        engine.pause_safe()
        result = engine.restart_worker(command.command_id)

        assert result["status"] == "PAUSED_SAFE"
        assert result["worker_restarted"] is False
        assert engine.store.read_state(command.command_id)["worker_pid"] == 424242
        assert killed == []
        assert spawned == []
    finally:
        engine.shutdown()


def test_pause_blocks_provider_wake_and_resume_recovers_deterministically(tmp_path, monkeypatch):
    engine = RuntimeEngine(_config(tmp_path))
    try:
        command = _command(engine, tmp_path)
        engine.store.write_state(
            command.command_id,
            state="WAITING_FOR_REASONING_PROVIDER",
            worker_pid=None,
            retry_after_epoch=0,
        )
        spawned = []
        monkeypatch.setattr(runtime_server, "pid_alive", lambda pid: False)
        monkeypatch.setattr(
            engine,
            "_spawn_worker",
            lambda command_id, recovered: spawned.append((command_id, recovered)) or 999,
        )

        engine.pause_safe()
        engine._wake_waiting_from_observed_provider_health()
        engine._recover_one(command.command_id)
        assert spawned == []

        result = engine.resume()
        assert result["status"] == "RESUMED"
        assert spawned == [(command.command_id, True)]
        assert engine.health()["paused"] is False
    finally:
        engine.shutdown()

def test_continuous_worker_stops_at_persisted_pause_cycle_boundary(
    tmp_path,
    monkeypatch,
):
    runtime_root = (
        tmp_path
        / "runtime"
    )

    descriptor = (
        tmp_path
        / "descriptor.json"
    )

    descriptor.write_text(
        "{}",
        encoding="utf-8",
    )

    policy = (
        tmp_path
        / "policy.json"
    )

    policy.write_text(
        "{}",
        encoding="utf-8",
    )

    workspace = (
        tmp_path
        / "workspace"
    )

    workspace.mkdir()

    profile = ProjectProfile(
        project_id="lari",
        descriptor_path=str(
            descriptor
        ),
        workspace=str(
            workspace
        ),
        routing_policy_path=str(
            policy
        ),
    )

    command = (
        ContinueProjectCommand
        .from_mapping(
            {
                "command_id":
                    "pause-cycle-boundary",

                "goal":
                    "continue",

                "continuous":
                    True,
            },
            project=profile,
        )
    )

    store = RuntimeStore(
        runtime_root
    )

    store.create_command(
        command.to_dict()
    )

    calls = []

    def fake_run_autonomous_project(
        **kwargs,
    ):
        calls.append(
            kwargs
        )

        # Model the real race that exposed the defect:
        # maintenance becomes PAUSED_SAFE while the current
        # continuous planning cycle is still in flight.
        persist_maintenance(
            runtime_root,
            paused=True,
            reason="test_cycle_boundary",
        )

        return {
            "disposition":
                "BOUNDED_RUN_EXHAUSTED",

            "completed_batch_count":
                1,

            "canonical_source_sha":
                "a" * 40,

            "canonical_execution_base_sha":
                "b" * 40,
        }

    monkeypatch.setattr(
        runtime_worker,
        "hydrate_environment",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        fake_run_autonomous_project,
    )

    result = (
        runtime_worker
        .execute_command(
            runtime_root,
            command.command_id,
        )
    )

    state = store.read_state(
        command.command_id
    )

    events = store.read_events(
        command.command_id
    )

    assert (
        len(calls)
        ==
        1
    )

    assert (
        read_maintenance(
            runtime_root
        )["state"]
        ==
        PAUSED_SAFE
    )

    assert (
        result["state"]
        ==
        "RUNNING"
    )

    assert (
        result["disposition"]
        ==
        "PAUSED_SAFE"
    )

    assert (
        result[
            "completed_batch_count"
        ]
        ==
        1
    )

    assert (
        state["state"]
        ==
        "RUNNING"
    )

    assert (
        state["disposition"]
        ==
        "PAUSED_SAFE"
    )

    assert (
        state["worker_pid"]
        is None
    )

    assert (
        state[
            "completed_batch_count"
        ]
        ==
        1
    )

    assert any(
        event.get(
            "event_type"
        )
        ==
        "runtime.worker_paused_safe"
        and
        event.get(
            "payload",
            {},
        ).get(
            "lineage_preserved"
        )
        is True
        for event in events
    )

