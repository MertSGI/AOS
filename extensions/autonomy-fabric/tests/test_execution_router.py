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
    AgenticExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    ExecutionTrustZone,
    EvidenceClass,
)
from extensions.autonomy_fabric.execution_router import (
    ExecutionRouter,
    summarize_backend_attempts,
)
from extensions.autonomy_fabric.agentic_planning_bridge import (
    AgenticStructuredPlanningBridge,
)
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


class _OrderedOrchestrator:
    def __init__(self, order):
        self.order = list(order)

    def select(self, backends, request):
        available = {backend.backend_id for backend in backends}
        return next((backend_id for backend_id in self.order if backend_id in available), None)


class _FakeAgenticBackend(AgenticExecutionBackend):
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED

    def __init__(self, backend_id, status="SUCCESS", output='{"title":"Plan","tasks":[]}'):
        self.backend_id = backend_id
        self.status = status
        self.output = output

    def get_health(self):
        return ExecutionHealth.HEALTHY

    def get_availability(self):
        return ExecutionAvailabilitySnapshot(
            state=ExecutionAvailabilityState.AVAILABLE,
            observed_at="",
            source="TEST_DOUBLE",
        )

    def start(self, request, context_pack):
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="test-double",
            task_id=request.task_id,
            request_id=request.request_id,
            status=self.status,
            exit_code=0 if self.status == "SUCCESS" else None,
            workspace=request.workspace,
            stdout_digest=self.output,
            evidence_payload={"raw_output": self.output},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )

    def resume(self, request, identity, context_pack):
        return self.start(request, context_pack)

    def interrupt(self, execution_id):
        return None


def _agentic_request(capability=ExecutionCapability.LONG_HORIZON_AGENTIC_WORK, payload=None):
    return ExecutionRequest(
        task_id="agentic-telemetry",
        project_id="telemetry-test",
        workspace=".",
        operation_class=capability.value,
        required_capabilities=[capability],
        authority_id="AUTH-TELEMETRY",
        payload=payload or {},
    )


def test_ag_direct_success_attempt_telemetry():
    router = ExecutionRouter(
        backends=[_FakeAgenticBackend("antigravity")],
        orchestrator=_OrderedOrchestrator(["antigravity"]),
    )
    result = router.execute_with_failover(_agentic_request())
    metrics = summarize_backend_attempts(router.last_attempt_telemetry)["antigravity"]

    assert result.status == "SUCCESS"
    assert metrics["attempt_count"] == 1
    assert metrics["direct_agentic_attempt_count"] == 1
    assert metrics["success_count"] == 1
    assert metrics["final_selection_count"] == 1


def test_ag_planning_bridge_underlying_attempt_telemetry():
    ag = _FakeAgenticBackend("antigravity")
    bridge = AgenticStructuredPlanningBridge(ag)
    router = ExecutionRouter(
        backends=[bridge],
        orchestrator=_OrderedOrchestrator([bridge.backend_id]),
    )
    request = _agentic_request(
        ExecutionCapability.MODEL_REASONING,
        payload={
            "prompt": "Plan",
            "schema": {
                "type": "object",
                "required": ["title", "tasks"],
                "properties": {
                    "title": {"type": "string"},
                    "tasks": {"type": "array"},
                },
            },
        },
    )
    result = router.execute_with_failover(request)
    metrics = summarize_backend_attempts(router.last_attempt_telemetry)

    assert result.status == "SUCCESS"
    assert metrics["antigravity"]["attempt_count"] == 1
    assert metrics["antigravity"]["planning_bridge_attempt_count"] == 1
    assert metrics["antigravity"]["success_count"] == 1
    assert metrics["antigravity"]["final_selection_count"] == 1
    assert metrics[bridge.backend_id]["attempt_count"] == 1


def test_ag_degraded_then_codex_success_attempt_telemetry():
    router = ExecutionRouter(
        backends=[
            _FakeAgenticBackend("antigravity", status="DEGRADED"),
            _FakeAgenticBackend("codex_cli"),
        ],
        orchestrator=_OrderedOrchestrator(["antigravity", "codex_cli"]),
    )
    result = router.execute_with_failover(_agentic_request())
    metrics = summarize_backend_attempts(router.last_attempt_telemetry)

    assert result.backend_id == "codex_cli"
    assert metrics["antigravity"]["attempt_count"] == 1
    assert metrics["antigravity"]["degraded_count"] == 1
    assert metrics["antigravity"]["final_selection_count"] == 0
    assert metrics["codex_cli"]["attempt_count"] == 1
    assert metrics["codex_cli"]["success_count"] == 1
    assert metrics["codex_cli"]["final_selection_count"] == 1


def test_no_ag_invocation_keeps_all_ag_attempt_counters_zero():
    router = ExecutionRouter(
        backends=[_FakeAgenticBackend("codex_cli")],
        orchestrator=_OrderedOrchestrator(["codex_cli"]),
    )
    result = router.execute_with_failover(_agentic_request())
    metrics = summarize_backend_attempts(router.last_attempt_telemetry)
    ag = metrics.get("antigravity", {})

    assert result.status == "SUCCESS"
    assert int(ag.get("attempt_count", 0)) == 0
    assert int(ag.get("direct_agentic_attempt_count", 0)) == 0
    assert int(ag.get("planning_bridge_attempt_count", 0)) == 0
    assert int(ag.get("success_count", 0)) == 0
    assert int(ag.get("degraded_count", 0)) == 0
    assert int(ag.get("final_selection_count", 0)) == 0


@pytest.mark.parametrize("backend_id", ["codex_cli", "cline"])
def test_generic_agentic_backend_attempt_telemetry(backend_id):
    router = ExecutionRouter(
        backends=[_FakeAgenticBackend(backend_id)],
        orchestrator=_OrderedOrchestrator([backend_id]),
    )

    result = router.execute_with_failover(_agentic_request())
    metrics = summarize_backend_attempts(router.last_attempt_telemetry)[backend_id]

    assert result.status == "SUCCESS"
    assert metrics == {
        "attempt_count": 1,
        "success_count": 1,
        "degraded_count": 0,
        "failure_count": 0,
        "final_selection_count": 1,
        "direct_agentic_attempt_count": 1,
        "planning_bridge_attempt_count": 0,
    }
