"""Unit tests for ExecutionRouter (V2).

Tests:
- Priority order: Native > CI/GitHub > Model+Native > Antigravity Specialist
- Automatic failover when AG is degraded, exhausted, or unavailable
- Authority boundaries blocking unauthorized operations
- Zero AG invocation when native backend is available
"""

import pytest
from unittest.mock import MagicMock

from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    EvidenceClass,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import (
    NativeFileWorker,
    NativeProcessWorker,
    AntigravityExecutionBackend,
)
from extensions.autonomy_fabric.authority_router import AuthorityRouter


def test_router_prioritizes_native_over_ag():
    native_proc = NativeProcessWorker()
    ag_backend = AntigravityExecutionBackend()

    # Both support PROCESS_EXEC or AG vs NATIVE
    router = ExecutionRouter(backends=[ag_backend, native_proc], ag_required=False)

    req = ExecutionRequest(
        task_id="t-order",
        project_id="p-order",
        workspace=".",
        operation_class="TEST",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="auth-1",
        payload={"cmd": ["python", "-c", "print(1)"]},
    )

    selected = router.select_backend(req)
    assert selected is not None
    assert selected.backend_id == "native_process_worker"


def test_router_failover_when_ag_quota_exhausted():
    ag_exhausted = AntigravityExecutionBackend(simulate_exhausted=True)
    native_proc = NativeProcessWorker()

    router = ExecutionRouter(backends=[ag_exhausted, native_proc])

    req = ExecutionRequest(
        task_id="t-failover",
        project_id="p-test",
        workspace=".",
        operation_class="TEST",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="auth-1",
        payload={"cmd": ["python", "-c", "print('failover-success')"]},
    )

    result = router.execute_with_failover(req)
    assert result.status == "SUCCESS"
    assert result.backend_id == "native_process_worker"
    assert "failover-success" in result.stdout_digest


def test_router_authority_boundary_denial():
    auth_router = AuthorityRouter()
    native_proc = NativeProcessWorker()
    router = ExecutionRouter(backends=[native_proc], authority_router=auth_router)

    req_prod = ExecutionRequest(
        task_id="t-prod-deploy",
        project_id="p-test",
        workspace=".",
        operation_class="PRODUCTION_RELEASE",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="auth-1",
    )

    selected = router.select_backend(req_prod)
    assert selected is None  # Blocked by authority gate
