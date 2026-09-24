import json

import pytest

from aos.context_pack import MAX_CONTEXT_PACK_BYTES, build_context_pack, handoff_seed
from extensions.autonomy_fabric.execution_backend import (
    AgenticSessionIdentity, ExecutionBackend, ExecutionCapability, ExecutionCost,
    ExecutionHealth, ExecutionRequest, ExecutionResult, ExecutionTrustZone,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter


def test_context_pack_is_bounded_content_bound_and_excludes_forbidden_keys():
    pack = build_context_pack(
        objective_id="obj", authority_id="auth", source_sha="a" * 40,
        workspace_fingerprint="b" * 64, checkpoint_id="cp",
        completed_work_unit_ids=["w1"], completed_work_unit_signatures={"w1": "c" * 64},
        artifact_hashes={"file.txt": "d" * 64}, remaining_work=["w2"],
        availability={"state": "AVAILABLE", "raw_error": "secret"},
        boundaries=["PRODUCTION_NO_GO"],
        read_context=[{"path": "x", "content_sha256": "e" * 64, "redacted_excerpt": "x" * 10000}],
    )
    assert len(json.dumps(pack).encode()) <= MAX_CONTEXT_PACK_BYTES + 100
    assert "raw_error" not in pack["availability"]
    assert handoff_seed(pack)["completed_work_unit_ids"] == ["w1"]


def test_context_pack_rejects_unbound_source_or_workspace():
    with pytest.raises(ValueError):
        build_context_pack(objective_id="o", authority_id="a", source_sha="bad", workspace_fingerprint="b" * 64, checkpoint_id="c")


def test_cross_resource_fallback_supersedes_session_and_carries_aos_owned_work():
    class CaptureBackend(ExecutionBackend):
        backend_id = "codex_cli"
        trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
        supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}
        cost = ExecutionCost.SUBSCRIPTION_INCLUDED
        captured = None
        def get_health(self): return ExecutionHealth.HEALTHY
        def execute(self, request):
            self.captured = request
            return ExecutionResult(
                backend_id=self.backend_id, worker_id="capture", task_id=request.task_id,
                request_id=request.request_id, status="SUCCESS", exit_code=0,
                workspace=request.workspace,
            )

    prior = AgenticSessionIdentity(
        resource_id="local_antigravity_subscription", backend_id="antigravity",
        session_or_thread_id="ag-session", workspace_fingerprint="b" * 64,
        source_sha="a" * 40, checkpoint_id="cp",
        completed_work_unit_ids=["done"], completed_work_unit_signatures={"done": "c" * 64},
        artifact_hashes={"x": "d" * 64},
    )
    request = ExecutionRequest(
        task_id="next", project_id="p", workspace=".", operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="auth", agentic_identity=prior,
    )
    backend = CaptureBackend()
    result = ExecutionRouter([backend]).execute_with_failover(request)
    assert result.status == "SUCCESS"
    assert backend.captured.agentic_identity is None
    assert backend.captured.context_pack["completed_work_unit_ids"] == ["done"]
    assert backend.captured.context_pack["superseded_session_ids"] == ["ag-session"]
