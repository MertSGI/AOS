import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_store import RuntimeStore
from aos import runtime_worker


def _command(tmp_path: Path):
    profile = ProjectProfile(
        project_id="lari",
        descriptor_path=str(tmp_path / "descriptor.json"),
        workspace=str(tmp_path / "workspace"),
        routing_policy_path=str(tmp_path / "policy.json"),
    )
    (tmp_path / "descriptor.json").write_text("{}", encoding="utf-8")
    (tmp_path / "policy.json").write_text("{}", encoding="utf-8")
    (tmp_path / "workspace").mkdir()
    return ContinueProjectCommand.from_mapping({"goal": "continue"}, project=profile)


def test_runtime_worker_persists_structured_failure_result(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path)
    store.create_command(command.to_dict())
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(runtime_worker, "run_autonomous_project", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        runtime_worker.execute_command(root, command.command_id)

    result = store.read_result(command.command_id)
    state = store.read_state(command.command_id)
    assert result["state"] == "FAILED"
    assert result["receipt"]["failure_class"] == "RUNTIME_EXECUTION_FAILURE"
    assert result["receipt"]["structured_runtime_failure"] is True
    assert state["failure_class"] == "RUNTIME_EXECUTION_FAILURE"
    assert store.read_events(command.command_id)[-1]["event_type"] == "run.failed"


def test_recovery_watcher_baselines_existing_artifacts_and_checkpoint(tmp_path):
    project_runtime = tmp_path / "project-runtime"
    project_runtime.mkdir()
    situation = project_runtime / "situation-0000.json"
    situation.write_text(json.dumps({"project_id": "lari"}), encoding="utf-8")
    checkpoint = project_runtime / "planning-kernel-checkpoint.json"
    checkpoint.write_text(json.dumps({"phase": "WAITING_FOR_REASONING_PROVIDER", "batch_number": 0}), encoding="utf-8")
    store = MagicMock()
    watcher = runtime_worker.PlanningArtifactWatcher(store, "command-1", project_runtime, threading.Event())

    watcher._emit_file(situation)
    watcher._emit_checkpoint()
    store.append_event.assert_not_called()

    checkpoint.write_text(json.dumps({"phase": "EXECUTING", "batch_number": 0}), encoding="utf-8")
    watcher._emit_checkpoint()
    store.append_event.assert_called_once()
    assert store.append_event.call_args.args[1] == "batch.executing"


def test_runtime_worker_maps_exact_canonical_binding_failure_to_human_required(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path)
    store.create_command(command.to_dict())
    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Missing required execution base SHA in canonical next_action_execution_base_sha")),
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["state"] == "HUMAN_REQUIRED"
    assert result["receipt"]["failure_class"] == "CANONICAL_CONTRADICTION"
    assert store.read_state(command.command_id)["state"] == "HUMAN_REQUIRED"
