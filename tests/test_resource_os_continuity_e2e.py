import json

from extensions.autonomy_fabric.execution_backend import (
    AgenticExecutionBackend, AgenticSessionIdentity, ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState, ExecutionCapability, ExecutionCost, ExecutionHealth,
    ExecutionRequest, ExecutionResult, ExecutionTrustZone,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter


class InjectedAgenticBackend(AgenticExecutionBackend):
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED

    def __init__(self, backend_id, state):
        self.backend_id = backend_id
        self.resource_id = f"resource-{backend_id}"
        self.state = state
        self.calls = []

    def get_availability(self):
        return ExecutionAvailabilitySnapshot(self.state, "2026-09-24T00:00:00+00:00", source="INJECTION")

    def get_health(self):
        return ExecutionHealth.HEALTHY if self.state == ExecutionAvailabilityState.AVAILABLE else ExecutionHealth.UNAVAILABLE

    def start(self, request, context_pack):
        self.calls.append(("start", request.task_id, dict(context_pack)))
        if request.task_id in context_pack.get("completed_work_unit_ids", []):
            return ExecutionResult(
                backend_id=self.backend_id, worker_id=self.backend_id, task_id=request.task_id,
                request_id=request.request_id, status="DEGRADED", exit_code=1,
                workspace=request.workspace, sanitized_errors=["COMPLETED_WORK_MUST_NOT_BE_DUPLICATED"],
            )
        completed = sorted(set(context_pack.get("completed_work_unit_ids", []) + [request.task_id]))
        identity = AgenticSessionIdentity(
            resource_id=self.resource_id, backend_id=self.backend_id,
            session_or_thread_id=f"session-{self.backend_id}-{len(self.calls)}",
            workspace_fingerprint="b" * 64, source_sha="a" * 40,
            checkpoint_id=request.request_id, completed_work_unit_ids=completed,
            completed_work_unit_signatures={item: "c" * 64 for item in completed},
            artifact_hashes=dict(context_pack.get("artifact_hashes", {})),
            superseded_session_ids=list(context_pack.get("superseded_session_ids", [])),
        )
        return ExecutionResult(
            backend_id=self.backend_id, worker_id=self.backend_id, task_id=request.task_id,
            request_id=request.request_id, status="SUCCESS", exit_code=0,
            workspace=request.workspace, agentic_identity=identity,
        )

    def resume(self, request, identity, context_pack):
        self.calls.append(("resume", request.task_id, identity.session_or_thread_id))
        return self.start(request, {
            "completed_work_unit_ids": identity.completed_work_unit_ids,
            "artifact_hashes": identity.artifact_hashes,
            "superseded_session_ids": identity.superseded_session_ids,
        })

    def interrupt(self, execution_id): pass


def _request(task_id, identity=None):
    return ExecutionRequest(
        task_id=task_id, project_id="project", workspace=".", operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="auth", agentic_identity=identity,
        context_pack={"completed_work_unit_ids": identity.completed_work_unit_ids if identity else []},
    )


def test_quota_loss_fallback_restart_reentry_preserves_work_without_stale_resume():
    ag = InjectedAgenticBackend("antigravity", ExecutionAvailabilityState.QUOTA_EXHAUSTED)
    codex = InjectedAgenticBackend("codex_cli", ExecutionAvailabilityState.AVAILABLE)
    router = ExecutionRouter([ag, codex])
    prior_ag = AgenticSessionIdentity(
        resource_id="resource-antigravity", backend_id="antigravity",
        session_or_thread_id="old-ag", workspace_fingerprint="b" * 64,
        source_sha="a" * 40, checkpoint_id="cp", completed_work_unit_ids=["done-1"],
        completed_work_unit_signatures={"done-1": "c" * 64}, artifact_hashes={"proof": "d" * 64},
    )

    fallback = router.execute_with_failover(_request("done-2", prior_ag))
    assert fallback.status == "SUCCESS" and fallback.backend_id == "codex_cli"
    assert ag.calls == []
    assert codex.calls[0][0] == "start"
    assert fallback.agentic_identity.completed_work_unit_ids == ["done-1", "done-2"]
    assert fallback.agentic_identity.superseded_session_ids == ["old-ag"]

    restarted = AgenticSessionIdentity(**json.loads(json.dumps(fallback.agentic_identity.to_dict())))
    codex.state = ExecutionAvailabilityState.QUOTA_EXHAUSTED
    ag.state = ExecutionAvailabilityState.AVAILABLE
    rerouted = ExecutionRouter([ag, codex]).execute_with_failover(_request("done-3", restarted))
    assert rerouted.status == "SUCCESS" and rerouted.backend_id == "antigravity"
    assert ag.calls[0][0] == "start"
    assert rerouted.agentic_identity.completed_work_unit_ids == ["done-1", "done-2", "done-3"]
    assert restarted.session_or_thread_id in rerouted.agentic_identity.superseded_session_ids

    duplicate = ExecutionRouter([ag]).execute_with_failover(_request("done-3", rerouted.agentic_identity))
    assert duplicate.status == "DEGRADED"
    assert duplicate.sanitized_errors == ["COMPLETED_WORK_MUST_NOT_BE_DUPLICATED"]
