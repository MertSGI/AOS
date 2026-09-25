"""Focused Tests for Agentic Structured Planning Bridge."""
import pytest
from extensions.autonomy_fabric.agentic_planning_bridge import (
    AgenticStructuredPlanningBridge,
    extract_json_object,
)
from extensions.autonomy_fabric.execution_backend import (
    AgenticExecutionBackend,
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)


class DummyAgenticBackend(AgenticExecutionBackend):
    backend_id = "dummy_agentic"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED

    def __init__(self, response_text: str = "", fail: bool = False, mutates: bool = False):
        self.response_text = response_text
        self.fail = fail
        self.mutates = mutates
        self.last_request = None

    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        return ExecutionAvailabilitySnapshot(
            state=ExecutionAvailabilityState.AVAILABLE,
            observed_at="",
            source="TEST",
        )

    def start(self, request: ExecutionRequest, context_pack: dict) -> ExecutionResult:
        self.last_request = request
        if self.fail:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="dummy",
                task_id=request.task_id,
                request_id=request.request_id,
                status="FAILED",
                exit_code=1,
                workspace=request.workspace,
                sanitized_errors=["DUMMY_ERROR"],
                evidence_payload={"failure_class": "DUMMY_ERROR"},
            )
        changed = ["mutated_file.txt"] if self.mutates else []
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="dummy",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            changed_paths=changed,
            stdout_digest=self.response_text,
            evidence_payload={"raw_output": self.response_text},
        )

    def resume(self, request, identity, context_pack):
        return self.start(request, context_pack)

    def interrupt(self, execution_id: str):
        pass


SCHEMA = {
    "type": "object",
    "required": ["title", "tasks"],
    "properties": {
        "title": {"type": "string"},
        "tasks": {"type": "array", "items": {"type": "string"}},
    },
}


def test_bridge_contract_and_capabilities():
    underlying = DummyAgenticBackend()
    bridge = AgenticStructuredPlanningBridge(underlying)
    assert bridge.backend_class == BackendClass.REASONING_BACKEND
    assert bridge.supported_capabilities == {ExecutionCapability.MODEL_REASONING}
    assert bridge.cost == ExecutionCost.SUBSCRIPTION_INCLUDED
    assert bridge.get_health() == ExecutionHealth.HEALTHY


def test_bridge_successful_structured_planning_request():
    valid_json = '{"title": "Autonomous Plan", "tasks": ["task-1", "task-2"]}'
    output = f"Here is the plan:\n```json\n{valid_json}\n```\nDone."
    underlying = DummyAgenticBackend(response_text=output)
    bridge = AgenticStructuredPlanningBridge(underlying)

    req = ExecutionRequest(
        task_id="plan-1",
        project_id="test",
        workspace=".",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "Create plan", "schema": SCHEMA},
    )

    res = bridge.execute(req)
    assert res.status == "SUCCESS"
    assert res.evidence_payload["proposal"]["title"] == "Autonomous Plan"
    assert res.evidence_payload["proposal"]["tasks"] == ["task-1", "task-2"]
    assert res.transient_structured_output == res.evidence_payload["proposal"]
    # Check underlying request received empty write_scope
    assert underlying.last_request.write_scope == []


def test_bridge_denies_write_authority():
    underlying = DummyAgenticBackend()
    bridge = AgenticStructuredPlanningBridge(underlying)

    req = ExecutionRequest(
        task_id="plan-write",
        project_id="test",
        workspace=".",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        write_scope=["src/"],  # Forbidden for planning
        payload={"prompt": "Create plan", "schema": SCHEMA},
    )

    res = bridge.execute(req)
    assert res.status == "DENIED"
    assert "PLANNING_WRITE_AUTHORITY_DENIED" in res.sanitized_errors


def test_bridge_fails_closed_on_mutation_by_agent():
    valid_json = '{"title": "Plan", "tasks": ["t1"]}'
    underlying = DummyAgenticBackend(response_text=valid_json, mutates=True)
    bridge = AgenticStructuredPlanningBridge(underlying)

    req = ExecutionRequest(
        task_id="plan-mut",
        project_id="test",
        workspace=".",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "Create plan", "schema": SCHEMA},
    )

    res = bridge.execute(req)
    assert res.status == "FAILED"
    assert "PLANNING_MUTATION_DETECTED" in res.sanitized_errors


def test_bridge_fails_closed_on_schema_violation():
    # Missing required field "tasks"
    invalid_json = '{"title": "Plan"}'
    underlying = DummyAgenticBackend(response_text=invalid_json)
    bridge = AgenticStructuredPlanningBridge(underlying)

    req = ExecutionRequest(
        task_id="plan-invalid-schema",
        project_id="test",
        workspace=".",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "Create plan", "schema": SCHEMA},
    )

    res = bridge.execute(req)
    assert res.status == "FAILED"
    assert "SCHEMA_VALIDATION_FAILED" in res.sanitized_errors
