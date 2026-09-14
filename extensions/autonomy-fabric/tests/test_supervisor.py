"""Unit tests for Parallel Supervisor (R3)."""

import pytest
import time
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.antigravity_adapter import FakeAntigravityAdapter, AntigravityStatus, ANTIGRAVITY_TO_AOS_STATUS_MAP
from extensions.autonomy_fabric.supervisor import (
    ParallelSupervisor,
    ConcurrencyLimitError,
    WorkspaceCollisionError,
    BranchCollisionError,
)


def test_supervisor_launch_and_concurrency_limit():
    registry = AgentRunRegistry()
    adapter = FakeAntigravityAdapter()
    adapter.default_status = AntigravityStatus.RUNNING  # Keep runs in RUNNING state
    supervisor = ParallelSupervisor(registry, adapter, max_concurrent_active_runs=2)

    run1 = supervisor.launch_run(
        project_id="p1",
        run_type="DEV",
        authority_id="auth-1",
        controller_id="ctrl-1",
        agent_provider="antigravity",
        workspace_path="/ws/1",
        branch="b1",
        initial_prompt="Task 1",
    )
    assert run1.status == RunStatus.RUNNING

    run2 = supervisor.launch_run(
        project_id="p2",
        run_type="DEV",
        authority_id="auth-2",
        controller_id="ctrl-1",
        agent_provider="antigravity",
        workspace_path="/ws/2",
        branch="b2",
        initial_prompt="Task 2",
    )
    assert run2.status == RunStatus.RUNNING

    # 2 active runs running (run1 and run2) -> launch 3rd must raise ConcurrencyLimitError
    with pytest.raises(ConcurrencyLimitError):
        supervisor.launch_run(
            project_id="p3",
            run_type="DEV",
            authority_id="auth-3",
            controller_id="ctrl-1",
            agent_provider="antigravity",
            workspace_path="/ws/3",
            branch="b3",
            initial_prompt="Task 3",
        )


def test_supervisor_collision_prevention():
    registry = AgentRunRegistry()
    supervisor = ParallelSupervisor(registry, FakeAntigravityAdapter(), max_concurrent_active_runs=4)

    run1 = supervisor.launch_run(
        project_id="p1",
        run_type="DEV",
        authority_id="auth-1",
        controller_id="ctrl-1",
        agent_provider="antigravity",
        workspace_path="/ws/shared",
        branch="feature/branch-a",
    )

    with pytest.raises(WorkspaceCollisionError):
        supervisor.launch_run(
            project_id="p2",
            run_type="DEV",
            authority_id="auth-2",
            controller_id="ctrl-1",
            agent_provider="antigravity",
            workspace_path="/ws/shared",
            branch="feature/branch-b",
        )

    with pytest.raises(BranchCollisionError):
        supervisor.launch_run(
            project_id="p3",
            run_type="DEV",
            authority_id="auth-3",
            controller_id="ctrl-1",
            agent_provider="antigravity",
            workspace_path="/ws/other",
            branch="feature/branch-a",
        )


def test_supervisor_stale_lease_reconciliation():
    registry = AgentRunRegistry()
    supervisor = ParallelSupervisor(registry, FakeAntigravityAdapter(), heartbeat_timeout_seconds=0.1)

    run = supervisor.launch_run(
        project_id="p1",
        run_type="DEV",
        authority_id="auth-1",
        controller_id="ctrl-1",
        agent_provider="antigravity",
        workspace_path="/ws/stale",
    )
    registry.transition(run.run_id, RunStatus.RUNNING)

    time.sleep(0.15)
    stale = supervisor.reconcile_stale_runs()
    assert run.run_id in stale
    assert registry.get_run(run.run_id).status == RunStatus.INTERRUPTED


def test_supervisor_crash_recovery():
    registry = AgentRunRegistry()
    supervisor = ParallelSupervisor(registry, FakeAntigravityAdapter())

    run = supervisor.launch_run(
        project_id="p1",
        run_type="DEV",
        authority_id="auth-1",
        controller_id="ctrl-1",
        agent_provider="antigravity",
        workspace_path="/ws/crash",
        branch="feature/crash",
    )
    registry.transition(run.run_id, RunStatus.RUNNING)

    # Simulate supervisor reboot with fresh instance
    new_supervisor = ParallelSupervisor(registry, FakeAntigravityAdapter())
    recovered = new_supervisor.recover_from_crash()

    assert recovered == 1
    assert "/ws/crash" in new_supervisor.workspace_locks
    assert "feature/crash" in new_supervisor.branch_locks


def test_supervisor_generic_routing_and_fail_closed_construction():
    from extensions.autonomy_fabric.supervisor import derive_run_capabilities, RunSupervisorError
    from extensions.autonomy_fabric.execution_backend import ExecutionCapability
    from extensions.autonomy_fabric.execution_router import ExecutionRouter
    from extensions.autonomy_fabric.native_workers import NativeFileWorker, NativeProcessWorker

    # 1. Capability derivation from run_type
    assert derive_run_capabilities("PLAN") == [ExecutionCapability.MODEL_REASONING]
    assert derive_run_capabilities("PATCH", {"action": "apply_patch"}) == [ExecutionCapability.PATCH_APPLY]
    assert derive_run_capabilities("FILE_WRITE", {"action": "write_file"}) == [ExecutionCapability.FILE_WRITE]
    assert derive_run_capabilities("GIT_COMMIT") == [ExecutionCapability.GIT_WRITE]
    assert derive_run_capabilities("BROWSER") == [ExecutionCapability.BROWSER]
    assert derive_run_capabilities("TEST") == [ExecutionCapability.PROCESS_EXEC]

    # 2. Production/runtime construction without router or adapter fails closed
    registry = AgentRunRegistry()
    with pytest.raises(RunSupervisorError) as exc:
        ParallelSupervisor(registry, adapter=None, router=None, allow_fake_adapter=False)
    assert "requires an eligible router or execution backend" in str(exc.value)

    # 3. Router dispatch using derived capabilities without AG adapter
    router = ExecutionRouter(backends=[NativeProcessWorker()])
    supervisor = ParallelSupervisor(registry, router=router, allow_fake_adapter=False)
    run = supervisor.launch_run(
        project_id="p-router",
        run_type="TEST",
        authority_id="auth-1",
        controller_id="ctrl-1",
        agent_provider="native_router",
        initial_prompt="Run python test",
        execution_payload={"cmd": ["python", "-c", "print(1)"]},
    )
    assert run.status == RunStatus.COMPLETED
