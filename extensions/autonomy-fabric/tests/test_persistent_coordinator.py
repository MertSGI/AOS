"""Unit tests for PersistentCoordinator and Checkpoint / Resume across restarts."""

import pytest
import os
import tempfile
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import NativeFileWorker, NativeProcessWorker
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
