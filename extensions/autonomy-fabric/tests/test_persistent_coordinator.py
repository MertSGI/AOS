"""Unit tests for PersistentCoordinator and Checkpoint / Resume across restarts."""

import pytest
import os
import tempfile
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import NativeFileWorker, NativeGitWorker, NativeProcessWorker
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator


def test_persistent_coordinator_batch_execution_and_checkpoint():
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint_path = os.path.join(tmpdir, "coordinator_state.json")
        registry = AgentRunRegistry()
        dag = TaskDAG("proj-coord-test", registry)

        n1 = dag.add_node("node-1", "DEV", "auth-1")
        n2 = dag.add_node("node-2", "TEST", "auth-1", dependencies=["node-1"])

        router = ExecutionRouter(backends=[NativeFileWorker(), NativeProcessWorker()])

        # Process A: Run first batch
        coord_a = PersistentCoordinator(
            project_id="proj-coord-test",
            workspace_path=tmpdir,
            dag=dag,
            router=router,
            registry=registry,
            checkpoint_file=checkpoint_path,
        )

        coord_a.execute_next_batch(max_tasks=1)
        assert "node-1" in coord_a.state.completed_task_ids
        assert os.path.exists(checkpoint_path)

        # Process B: Reboot coordinator from checkpoint
        coord_b = PersistentCoordinator(
            project_id="proj-coord-test",
            workspace_path=tmpdir,
            dag=dag,
            router=router,
            registry=registry,
            checkpoint_file=checkpoint_path,
        )

        assert "node-1" in coord_b.state.completed_task_ids
        # Node 2 is now eligible and uncompleted
        results_b = coord_b.execute_next_batch(max_tasks=1)
        assert len(results_b) == 1
        assert "node-2" in coord_b.state.completed_task_ids
        assert dag.compute_progress() == 100.0


def test_canonical_git_run_type_routes_to_native_git_worker(tmp_path):
    registry = AgentRunRegistry()
    dag = TaskDAG("proj-git", registry)
    node = dag.add_node("git-version", "GIT", "auth-1")
    node.payload = {"action": "--version", "args": []}
    router = ExecutionRouter(backends=[NativeProcessWorker(), NativeGitWorker()])
    coord = PersistentCoordinator(
        project_id="proj-git",
        workspace_path=str(tmp_path),
        dag=dag,
        router=router,
        registry=registry,
    )

    result = coord.execute_next_batch(max_tasks=1)[0]

    assert result.backend_id == "native_git_worker"
    assert "git-version" in coord.state.completed_task_ids


def test_failed_task_is_not_retried_within_same_bounded_run(tmp_path):
    registry = AgentRunRegistry()
    dag = TaskDAG("proj-fail", registry)
    node = dag.add_node("bad-process", "PROCESS", "auth-1")
    node.payload = {"cmd": ["definitely-not-allowed"]}
    router = ExecutionRouter(backends=[NativeProcessWorker()])
    coord = PersistentCoordinator(
        project_id="proj-fail",
        workspace_path=str(tmp_path),
        dag=dag,
        router=router,
        registry=registry,
    )

    state = coord.run_until_complete(max_iterations=30)

    assert state.iteration_count == 1
    assert state.failed_task_ids == ["bad-process"]
