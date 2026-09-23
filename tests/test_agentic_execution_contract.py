import datetime

from extensions.autonomy_fabric.execution_backend import (
    AgenticSessionIdentity,
    BackendClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionCost,
    ExecutionRequest,
)


def test_agentic_contract_serializes_typed_identity_and_availability():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    identity = AgenticSessionIdentity(
        resource_id="subscription-resource",
        backend_id="agentic-backend",
        session_or_thread_id="session-1",
        workspace_fingerprint="a" * 64,
        source_sha="b" * 40,
        checkpoint_id="checkpoint-1",
        objective_id="objective-1",
    )
    availability = ExecutionAvailabilitySnapshot(
        state=ExecutionAvailabilityState.LOW_OR_SCARCE,
        observed_at=now,
        retry_after_epoch=1234.0,
        evidence={"used_percent": 90},
    )
    request = ExecutionRequest(
        task_id="task-1",
        project_id="project-1",
        workspace=".",
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="authority-1",
        agentic_identity=identity,
        context_pack={"checkpoint_id": "checkpoint-1"},
    )

    serialized = request.to_dict()
    assert serialized["agentic_identity"]["session_or_thread_id"] == "session-1"
    assert serialized["required_capabilities"] == ["LONG_HORIZON_AGENTIC_WORK"]
    assert availability.to_dict()["state"] == "LOW_OR_SCARCE"
    assert BackendClass.AGENTIC_EXECUTION_BACKEND.value == "AGENTIC_EXECUTION_BACKEND"
    assert ExecutionCost.SUBSCRIPTION_INCLUDED.value == "SUBSCRIPTION_INCLUDED"
