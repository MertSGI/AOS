"""Unit tests for PersistentCoordinator and Checkpoint / Resume across restarts."""

import pytest
import os
import tempfile
import json
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import NativeFileWorker, NativeGitWorker, NativeProcessWorker
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator
from extensions.autonomy_fabric.persistent_coordinator import AGENTIC_EXECUTION_TIMEOUT_SECONDS


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


def test_completed_read_observation_is_hash_bound_redacted_and_restartable(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("safe prefix OPENAI_API_KEY=sk-forbidden-value\n", encoding="utf-8")
    checkpoint = tmp_path / "coordinator.json"
    generation = "a" * 64
    registry = AgentRunRegistry()
    dag = TaskDAG("proj-read", registry)
    node = dag.add_node("read-doc", "FILE", "auth-1")
    node.payload = {"action": "read_file", "path": "README.md"}
    router = ExecutionRouter(backends=[NativeFileWorker()])
    coordinator = PersistentCoordinator(
        project_id="proj-read",
        workspace_path=str(tmp_path),
        dag=dag,
        router=router,
        registry=registry,
        checkpoint_file=str(checkpoint),
        workspace_source_generation=generation,
    )

    result = coordinator.execute_next_batch(max_tasks=1)[0]
    assert result.status == "SUCCESS"
    assert len(coordinator.state.completed_read_observations) == 1
    observation = coordinator.state.completed_read_observations[0]
    assert observation["normalized_path"] == "readme.md"
    assert observation["workspace_source_generation"] == generation
    assert len(observation["content_sha256"]) == 64
    assert len(observation["read_identity"]) == 64
    assert "sk-forbidden-value" not in json.dumps(observation)

    restarted = PersistentCoordinator(
        project_id="proj-read",
        workspace_path=str(tmp_path),
        dag=dag,
        router=router,
        registry=registry,
        checkpoint_file=str(checkpoint),
        workspace_source_generation=generation,
    )
    assert restarted.state.completed_read_observations == [observation]


def test_waiting_for_reasoning_provider_is_non_terminal_and_does_not_fail_task(tmp_path):
    from extensions.autonomy_fabric.execution_backend import (
        ExecutionBackend, ExecutionCapability, ExecutionCost, ExecutionResult,
        ExecutionTrustZone,
    )

    class WaitingBackend(ExecutionBackend):
        backend_id = "waiting_test_backend"
        trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
        cost = ExecutionCost.FREE_TIER_CLOUD
        supported_capabilities = {ExecutionCapability.MODEL_REASONING}

        def get_health(self):
            from extensions.autonomy_fabric.execution_backend import ExecutionHealth
            return ExecutionHealth.HEALTHY

        def execute(self, request):
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="test_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="WAITING_FOR_REASONING_PROVIDER",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["PROVIDER_WAIT_RESOURCE_UNAVAILABLE"],
            )

    registry = AgentRunRegistry()
    dag = TaskDAG("proj-wait-test", registry)
    dag.add_node("plan-task", "REASONING", "auth-1")

    router = ExecutionRouter(backends=[WaitingBackend()])
    checkpoint_file = tmp_path / "coordinator-checkpoint.json"
    coordinator = PersistentCoordinator(
        project_id="proj-wait-test",
        workspace_path=str(tmp_path),
        dag=dag,
        router=router,
        registry=registry,
        checkpoint_file=str(checkpoint_file),
    )

    results = coordinator.execute_next_batch(max_tasks=1)
    assert len(results) == 1
    assert results[0].status == "WAITING_FOR_REASONING_PROVIDER"
    # Invariant: Must NOT be in failed_task_ids
    assert "plan-task" not in coordinator.state.failed_task_ids
    assert "plan-task" not in coordinator.state.completed_task_ids
    runs = registry.list_runs(project_id="proj-wait-test")
    assert len(runs) == 1
    assert runs[0].status == RunStatus.WAITING_AGENT


def test_agentic_tasks_receive_bounded_long_horizon_timeout(tmp_path):
    from extensions.autonomy_fabric.execution_backend import (
        ExecutionBackend, ExecutionCapability, ExecutionCost, ExecutionHealth,
        ExecutionResult, ExecutionTrustZone,
    )

    class CapturingAgenticBackend(ExecutionBackend):
        backend_id = "capturing_agentic"
        trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
        cost = ExecutionCost.FREE_LOCAL
        supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}

        def __init__(self):
            self.requests = []

        def get_health(self):
            return ExecutionHealth.HEALTHY

        def execute(self, request):
            self.requests.append(request)
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="capture",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
            )

    registry = AgentRunRegistry()
    dag = TaskDAG("proj-agentic-timeout", registry)
    node = dag.add_node("agentic-work", "AGENTIC", "auth-1")
    node.payload = {"prompt": "Perform bounded agentic work"}
    backend = CapturingAgenticBackend()
    coordinator = PersistentCoordinator(
        project_id="proj-agentic-timeout",
        workspace_path=str(tmp_path),
        dag=dag,
        router=ExecutionRouter(backends=[backend]),
        registry=registry,
    )

    result = coordinator.execute_next_batch(max_tasks=1)[0]

    assert result.status == "SUCCESS"
    assert backend.requests[0].timeout_seconds == AGENTIC_EXECUTION_TIMEOUT_SECONDS
