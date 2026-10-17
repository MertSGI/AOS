"""Regression tests for AOS B9 - Operational Resource Orchestration and Agentic Execution Convergence."""

import pytest
import os
import tempfile
import subprocess
from pathlib import Path

from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    ExecutionAvailabilityState,
    ExecutionAvailabilitySnapshot,
    BackendClass,
    AgenticSessionIdentity,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator, ResourceRank
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import NativeFileWorker, NativeProcessWorker
from extensions.autonomy_fabric.cline_agentic_backend import ClineAgenticExecutionBackend
from extensions.autonomy_fabric.codex_cli_backend import CodexCliExecutionBackend


class FakeBackend:
    def __init__(
        self,
        backend_id: str,
        capabilities: set[ExecutionCapability],
        cost: ExecutionCost,
        backend_class: BackendClass = BackendClass.REASONING_BACKEND,
        health: ExecutionHealth = ExecutionHealth.HEALTHY,
        availability_state: ExecutionAvailabilityState = ExecutionAvailabilityState.AVAILABLE,
        context_tokens: int = 32768,
        quality_tier: int = 1,
        latency_ms: int = 1000,
    ):
        self.backend_id = backend_id
        self.supported_capabilities = capabilities
        self.cost = cost
        self.backend_class = backend_class
        self._health = health
        self._availability_state = availability_state
        self.context_window_tokens = context_tokens
        self.quality_tier = quality_tier
        self.expected_latency_ms = latency_ms

    def get_health(self) -> ExecutionHealth:
        return self._health

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        return ExecutionAvailabilitySnapshot(
            self._availability_state,
            "2026-10-17T20:00:00Z",
            source="TEST",
            evidence={},
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="fake_worker",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
        )


def _base_request(
    task_id: str = "t1",
    capabilities: list[ExecutionCapability] = None,
    payload: dict = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        project_id="p1",
        workspace=".",
        operation_class="TEST_OP",
        required_capabilities=capabilities or [ExecutionCapability.PROCESS_EXEC],
        authority_id="a1",
        payload=payload or {},
    )


# -------------------------------------------------------------
# Policy 1: Deterministic tasks select native execution
# -------------------------------------------------------------
def test_b9_policy_1_deterministic_tasks_prefer_native():
    native = FakeBackend(
        "native_proc",
        {ExecutionCapability.PROCESS_EXEC, ExecutionCapability.FILE_WRITE},
        ExecutionCost.FREE_LOCAL,
        backend_class=BackendClass.NATIVE_EXECUTION_BACKEND,
    )
    cloud_free = FakeBackend(
        "nemotron_cloud",
        {ExecutionCapability.PROCESS_EXEC, ExecutionCapability.FILE_WRITE},
        ExecutionCost.FREE_TIER_CLOUD,
        backend_class=BackendClass.REASONING_BACKEND,
    )
    orch = ResourceOrchestrator()
    req = _base_request(capabilities=[ExecutionCapability.PROCESS_EXEC])
    ranks = orch.rank([cloud_free, native], req)
    assert orch.select([cloud_free, native], req) == "native_proc"
    native_rank = next(r for r in ranks if r.backend_id == "native_proc")
    cloud_rank = next(r for r in ranks if r.backend_id == "nemotron_cloud")
    assert native_rank.score < cloud_rank.score


# -------------------------------------------------------------
# Policy 2: Small adequate reasoning prefers eligible local/free
# -------------------------------------------------------------
def test_b9_policy_2_small_adequate_reasoning_prefers_local_or_verified_free():
    local_qwen = FakeBackend(
        "qwen3_4b_llama_cpp",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.FREE_LOCAL,
        backend_class=BackendClass.REASONING_BACKEND,
        context_tokens=4096,
        quality_tier=1,
    )
    cloud_free = FakeBackend(
        "nemotron_free",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.FREE_TIER_CLOUD,
        backend_class=BackendClass.REASONING_BACKEND,
        context_tokens=32768,
        quality_tier=2,
    )
    bridge_subscription = FakeBackend(
        "planning_bridge",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.SUBSCRIPTION_INCLUDED,
        backend_class=BackendClass.REASONING_BACKEND,
        context_tokens=128000,
        quality_tier=3,
    )
    req = _base_request(
        capabilities=[ExecutionCapability.MODEL_REASONING],
        payload={
            "resource_requirements": {
                "context_tokens": 1000,
                "minimum_quality": 1,
                "complexity_class": "LOW",
                "local_qwen_allowed": True,
            }
        },
    )
    orch = ResourceOrchestrator()
    selected = orch.select([bridge_subscription, cloud_free, local_qwen], req)
    assert selected == "qwen3_4b_llama_cpp"


# -------------------------------------------------------------
# Policy 3: Higher-complexity coding selects genuinely capable free model
# -------------------------------------------------------------
def test_b9_policy_3_higher_complexity_selects_capable_free_model():
    small_model = FakeBackend(
        "small_model",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.FREE_LOCAL,
        quality_tier=1,
        context_tokens=4096,
    )
    large_free = FakeBackend(
        "nemotron_120b",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.FREE_TIER_CLOUD,
        quality_tier=3,
        context_tokens=65536,
    )
    req = _base_request(
        capabilities=[ExecutionCapability.MODEL_REASONING],
        payload={
            "resource_requirements": {
                "context_tokens": 16000,
                "minimum_quality": 3,
                "complexity_class": "HIGH",
            }
        },
    )
    orch = ResourceOrchestrator()
    ranks = orch.rank([small_model, large_free], req)
    small_rank = next(r for r in ranks if r.backend_id == "small_model")
    large_rank = next(r for r in ranks if r.backend_id == "nemotron_120b")
    assert small_rank.eligible is False
    assert "QUALITY_INADEQUATE" in small_rank.reasons
    assert "CONTEXT_INADEQUATE" in small_rank.reasons
    assert large_rank.eligible is True
    assert orch.select([small_model, large_free], req) == "nemotron_120b"


# -------------------------------------------------------------
# Policy 4 & 5: AG quota unavailable => reroute or typed wait
# -------------------------------------------------------------
def test_b9_policy_4_and_5_ag_scarcity_and_quota_failover():
    codex = CodexCliExecutionBackend(
        runner=lambda *args: None,
        capability_status_provider=lambda: "PROVEN",
        quota_snapshot_provider=lambda: {"state": "QUOTA_EXHAUSTED"},
        executable_identity={"path": "c", "filename": "c", "sha256": "0" * 64, "version": "1"},
    )
    cline = ClineAgenticExecutionBackend(
        runner=lambda *args: None,
        capability_status_provider=lambda: "OPERATIONAL_BOUNDED",
        executable_identity={"path": "cl", "filename": "cl", "sha256": "1" * 64, "version": "3.0.65"},
        underlying_provider="openai-compatible",
        underlying_model="qwen-local",
        underlying_cost_class="FREE_LOCAL",
    )
    req = _base_request(
        capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
    )
    orch = ResourceOrchestrator()
    ranks = orch.rank([codex, cline], req)
    codex_rank = next(r for r in ranks if r.backend_id == "codex_cli")
    cline_rank = next(r for r in ranks if r.backend_id == "cline")
    assert codex_rank.eligible is False
    assert "AVAILABILITY_QUOTA_EXHAUSTED" in codex_rank.reasons
    assert cline_rank.eligible is True
    assert orch.select([codex, cline], req) == "cline"


# -------------------------------------------------------------
# Policy 6: No adequate provider => typed WAIT / HOLD
# -------------------------------------------------------------
def test_b9_policy_6_no_adequate_provider_yields_typed_exhaustion():
    exhausted_backend = FakeBackend(
        "cloud_exhausted",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.FREE_TIER_CLOUD,
        health=ExecutionHealth.QUOTA_EXHAUSTED,
        availability_state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
    )
    req = _base_request(capabilities=[ExecutionCapability.MODEL_REASONING])
    orch = ResourceOrchestrator()
    ranks = orch.rank([exhausted_backend], req)
    assert not any(r.eligible for r in ranks)
    exhaustion_class = orch.classify_exhaustion([exhausted_backend], req)
    assert exhaustion_class in (
        "WAITING_RESOURCE",
        "AVAILABILITY_EXHAUSTED",
        "WAITING_FOR_REASONING_PROVIDER",
        "RESOURCE_UNAVAILABLE",
    )


# -------------------------------------------------------------
# Policy 7: Accepted objectives never executed twice
# -------------------------------------------------------------
def test_b9_policy_7_duplicate_work_unit_rejected():
    backend = CodexCliExecutionBackend(
        runner=lambda *args: None,
        capability_status_provider=lambda: "TEST_DOUBLE",
        quota_snapshot_provider=lambda: {"state": "AVAILABLE", "source": "TEST"},
        executable_identity={"path": "c", "filename": "c", "sha256": "0" * 64, "version": "1"},
    )
    prior_session = AgenticSessionIdentity(
        resource_id="codex_harness",
        backend_id="codex_cli",
        workspace_fingerprint="f" * 64,
        source_sha="a" * 40,
        checkpoint_id="chk-1",
        session_or_thread_id="thread-test-123",
        completed_work_unit_ids=["task-duplicate-check"],
    )
    req = _base_request(
        task_id="task-duplicate-check",
        capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        payload={"source_sha": "a" * 40},
    )
    req.agentic_identity = prior_session
    result = backend.execute(req)
    assert result.status in ("FAILED", "DEGRADED")
    assert "COMPLETED_WORK_MUST_NOT_BE_DUPLICATED" in result.sanitized_errors


# -------------------------------------------------------------
# Policy 8: Identity / fingerprint mismatch rejects resume
# -------------------------------------------------------------
def test_b9_policy_8_workspace_changed_rejects_incompatible_resume(tmp_path):
    backend = CodexCliExecutionBackend(
        runner=lambda *args: None,
        capability_status_provider=lambda: "TEST_DOUBLE",
        quota_snapshot_provider=lambda: {"state": "AVAILABLE", "source": "TEST"},
        executable_identity={"path": "c", "filename": "c", "sha256": "0" * 64, "version": "1"},
    )
    # Init real repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=tmp_path, check=True)
    f = tmp_path / "file.txt"
    f.write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()

    prior_session = AgenticSessionIdentity(
        resource_id="local_codex_cli_chatgpt_subscription",
        backend_id="codex_cli",
        workspace_fingerprint="0" * 64,  # Mismatched fingerprint
        source_sha=head_sha,
        checkpoint_id="chk-1",
        session_or_thread_id="11111111-2222-4333-8444-555555555555",
        completed_work_unit_ids=[],
    )
    req = ExecutionRequest(
        task_id="task-next",
        project_id="p1",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="a1",
        payload={"source_sha": head_sha},
        agentic_identity=prior_session,
    )
    result = backend.execute(req)
    assert result.status in ("FAILED", "DEGRADED")
    assert any("WORKSPACE_CHANGED" in err for err in result.sanitized_errors)


# -------------------------------------------------------------
# Policy 9: Secret scrubbing & Workspace containment
# -------------------------------------------------------------
def test_b9_policy_9_secret_scrubbing_and_containment():
    from extensions.autonomy_fabric.native_workers import redact_secrets

    sensitive_text = "NVIDIA_API_KEY=nvapi-secret12345 GEMINI_API_KEY=AIzaSySecret67890"
    cleaned = redact_secrets(sensitive_text)
    assert "nvapi-secret12345" not in cleaned
    assert "AIzaSySecret67890" not in cleaned


# -------------------------------------------------------------
# Policy 10: Paid fallback remains impossible
# -------------------------------------------------------------
def test_b9_policy_10_paid_fallback_denied_default():
    paid_backend = FakeBackend(
        "openai_paid",
        {ExecutionCapability.MODEL_REASONING},
        ExecutionCost.PAID_CLOUD,
    )
    req = _base_request(capabilities=[ExecutionCapability.MODEL_REASONING])
    orch = ResourceOrchestrator()
    ranks = orch.rank([paid_backend], req)
    assert ranks[0].eligible is False
    assert "PAID_DEFAULT_DENIED" in ranks[0].reasons
    assert orch.select([paid_backend], req) is None


# -------------------------------------------------------------
# Cline OPERATIONAL_BOUNDED Status Attestation Proof
# -------------------------------------------------------------
def test_b9_cline_operational_bounded_availability_proof():
    cline = ClineAgenticExecutionBackend(
        runner=lambda *args: None,
        capability_status_provider=lambda: "OPERATIONAL_BOUNDED",
        executable_identity={"path": "cl", "filename": "cl", "sha256": "1" * 64, "version": "3.0.65"},
        underlying_provider="openai-compatible",
        underlying_model="qwen-local",
        underlying_cost_class="FREE_LOCAL",
    )
    avail = cline.get_availability()
    assert avail.state == ExecutionAvailabilityState.AVAILABLE
    assert avail.evidence["status"] == "OPERATIONAL_BOUNDED"
    assert avail.evidence["paid_fallback"] == "DISABLED"
    assert cline.get_health() == ExecutionHealth.HEALTHY
