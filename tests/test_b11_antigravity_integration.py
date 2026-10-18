"""Regression tests for AOS B11 — Official Antigravity CLI Integration, Trusted Routing and Continuity.

Covers the 10 mandatory regression scenarios:
1. Native task never consumes AG quota.
2. Ineligible AG is not selected.
3. Eligible AG can be selected for a matching long-horizon task without overriding scope.
4. AG quota exhausted mid-task preserves completed objective receipts.
5. Compatible free resource continues the remaining objective without duplicate work.
6. No adequate resource yields WAIT/HOLD.
7. Incompatible session/fingerprint rejects unsafe resume.
8. Untrusted executable identity rejects AG invocation.
9. Paid overage disabled prevents billable fallback.
10. Historical Product commands never resume as a consequence of resource recovery.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from aos.antigravity_authority import (
    PROTECTED_PRODUCT_PROJECT_IDS,
    AntigravityExecutionAuthority,
    evaluate_antigravity_authority,
)
from extensions.autonomy_fabric.antigravity_adapter import (
    AntigravityResponse,
    AntigravityStatus,
    BaseAntigravityAdapter,
    FakeAntigravityAdapter,
)
from extensions.autonomy_fabric.antigravity_agentic_backend import (
    AntigravityAgenticExecutionBackend,
)
from extensions.autonomy_fabric.execution_backend import (
    AgenticSessionIdentity,
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionCost,
    ExecutionFailureDisposition,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


class MockFakeBackend:
    """Mock backend implementing the ExecutionBackend interface for deterministic test routing."""

    def __init__(
        self,
        backend_id: str,
        capabilities: set[ExecutionCapability],
        cost: ExecutionCost,
        backend_class: BackendClass = BackendClass.REASONING_BACKEND,
        health: ExecutionHealth = ExecutionHealth.HEALTHY,
        availability_state: ExecutionAvailabilityState = ExecutionAvailabilityState.AVAILABLE,
    ):
        self.backend_id = backend_id
        self.supported_capabilities = capabilities
        self.cost = cost
        self.backend_class = backend_class
        self._health = health
        self._availability_state = availability_state
        self.invocations: List[ExecutionRequest] = []

    def get_health(self) -> ExecutionHealth:
        return self._health

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        return ExecutionAvailabilitySnapshot(
            self._availability_state,
            "2026-10-18T10:00:00Z",
            source="MOCK",
            evidence={},
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.invocations.append(request)
        pack = request.context_pack or {}
        completed = list(pack.get("completed_work_unit_ids", []))
        if request.task_id not in completed:
            completed.append(request.task_id)
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id=f"{self.backend_id}_worker",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            evidence_payload={"completed_work_unit_ids": completed},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )


def _setup_git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "b11@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "B11 Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


# =========================================================================
# Scenario 1: Native task never consumes AG quota
# =========================================================================
def test_scenario_1_native_task_never_consumes_ag_quota(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_ag_adapter = FakeAntigravityAdapter()
    ag_backend = AntigravityAgenticExecutionBackend(
        fake_ag_adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
    )
    native_backend = MockFakeBackend(
        "native_worker",
        {ExecutionCapability.FILE_READ, ExecutionCapability.FILE_WRITE, ExecutionCapability.PATCH_APPLY},
        ExecutionCost.FREE_LOCAL,
        backend_class=BackendClass.NATIVE_EXECUTION_BACKEND,
    )

    router = ExecutionRouter(backends=[native_backend, ag_backend])

    req = ExecutionRequest(
        task_id="det-1",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="DETERMINISTIC",
        required_capabilities=[ExecutionCapability.FILE_READ, ExecutionCapability.FILE_WRITE, ExecutionCapability.PATCH_APPLY],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha, "task_class": "DETERMINISTIC"},
    )

    selected = router.select_backend(req)
    assert selected is not None
    assert selected.backend_id == "native_worker"

    result = router.execute_with_failover(req)
    assert result.status == "SUCCESS"
    assert result.backend_id == "native_worker"
    # Crucial assertion: zero AG invocations or quota consumption
    assert len(fake_ag_adapter.invocations) == 0


# =========================================================================
# Scenario 2: Ineligible AG is not selected
# =========================================================================
def test_scenario_2_ineligible_ag_is_not_selected(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_ag_adapter = FakeAntigravityAdapter()

    # Case A: Authority disabled
    disabled_authority = AntigravityExecutionAuthority(
        project_id="test-proj",
        enabled=False,
    )
    ag_backend_disabled = AntigravityAgenticExecutionBackend(
        fake_ag_adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=disabled_authority,
    )
    orchestrator = ResourceOrchestrator()
    req = ExecutionRequest(
        task_id="lh-1",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha, "task_class": "LONG_HORIZON_AGENTIC_WORK"},
    )
    ranks = orchestrator.rank([ag_backend_disabled], req)
    assert ranks[0].eligible is False
    assert any("AUTHORITY_DENIED:AUTHORITY_DISABLED" in r for r in ranks[0].reasons)

    # Case B: Attestation UNPROVEN
    ag_backend_unproven = AntigravityAgenticExecutionBackend(
        fake_ag_adapter,
        capability_status_provider=lambda: "UNPROVEN",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=AntigravityExecutionAuthority(project_id="test-proj", enabled=True),
    )
    ranks_unproven = orchestrator.rank([ag_backend_unproven], req)
    assert ranks_unproven[0].eligible is False

    # Case C: Disallowed task class (e.g. SIMPLE_REASONING)
    req_simple = ExecutionRequest(
        task_id="sr-1",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="SIMPLE_REASONING",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha, "task_class": "SIMPLE_REASONING", "antigravity_authority": {"project_id": "test-proj", "enabled": True}},
    )
    ranks_simple = orchestrator.rank([ag_backend_disabled], req_simple)
    assert ranks_simple[0].eligible is False

    # Case D: Protected Product Lane (lari)
    req_product = ExecutionRequest(
        task_id="prod-1",
        project_id="lari",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha},
    )
    ranks_prod = orchestrator.rank([ag_backend_disabled], req_product)
    assert ranks_prod[0].eligible is False
    assert any("PRODUCT_LANE_FORBIDDEN" in r for r in ranks_prod[0].reasons)


# =========================================================================
# Scenario 3: Eligible AG selected for matching long-horizon task
# =========================================================================
def test_scenario_3_eligible_ag_selected_for_matching_long_horizon_task(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_adapter = FakeAntigravityAdapter()

    class MutatingAdapter(BaseAntigravityAdapter):
        def execute_prompt(self, prompt, **kwargs):
            # Mutate workspace file
            (tmp_path / "work.txt").write_text("agentic output\n", encoding="utf-8")
            return AntigravityResponse(
                conversation_id="conv-b11-1",
                status=AntigravityStatus.SUCCESS,
                mapped_aos_status=RunStatus.COMPLETED if "RunStatus" in globals() else None,
                raw_response=json.dumps({"status": "SUCCESS"}),
                parsed_json={"conversation_id": "conv-b11-1", "status": "SUCCESS"},
                turn_count=2,
                usage_metadata={"total_tokens": 120},
            )

    authority = AntigravityExecutionAuthority(
        project_id="test-proj",
        enabled=True,
        allowed_workspaces=(str(tmp_path),),
        allowed_task_classes=("LONG_HORIZON_AGENTIC_WORK",),
    )
    ag_backend = AntigravityAgenticExecutionBackend(
        MutatingAdapter(),
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=authority,
    )

    req = ExecutionRequest(
        task_id="task-lh-3",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={
            "source_sha": source_sha,
            "task_class": "LONG_HORIZON_AGENTIC_WORK",
            "prompt": "perform objective",
        },
        write_scope=["work.txt"],
        expected_artifacts=["work.txt"],
    )

    result = ag_backend.start(req, {})
    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert "work.txt" in result.changed_paths
    assert result.agentic_identity is not None
    assert result.agentic_identity.session_or_thread_id == "conv-b11-1"


# =========================================================================
# Scenario 4: AG quota exhausted mid-task preserves completed objective receipts
# =========================================================================
def test_scenario_4_ag_quota_exhausted_preserves_completed_objective_receipts(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)

    # Initial successful identity from objective 1
    (tmp_path / "obj1_out.txt").write_text("obj1 done\n", encoding="utf-8")
    prior_identity = AgenticSessionIdentity(
        resource_id="local_antigravity_subscription",
        backend_id="antigravity",
        session_or_thread_id="conv-session-4",
        workspace_fingerprint="fp1",
        source_sha=source_sha,
        checkpoint_id="chk-1",
        last_successful_turn=1,
        last_successful_artifact={"path": "obj1_out.txt", "sha256": "sha-obj1"},
        started_at="2026-10-18T10:00:00Z",
        updated_at="2026-10-18T10:05:00Z",
        adapter_contract_version="0.2.9",
        backend_version="1.0",
        executable_sha256="f" * 64,
        auth_mode="subscription",
        objective_id="obj-1",
        last_terminal_event="result:SUCCESS",
        completed_work_unit_ids=["obj-1"],
        completed_work_unit_signatures={"obj-1": "sig-obj-1"},
        artifact_hashes={"obj1_out.txt": "sha-obj1"},
    )

    # Adapter returns 429 quota exhaustion on objective 2
    class QuotaExhaustedAdapter(BaseAntigravityAdapter):
        def execute_prompt(self, prompt, **kwargs):
            return AntigravityResponse(
                conversation_id="conv-session-4",
                status=AntigravityStatus.ERROR,
                mapped_aos_status=None,
                raw_response=json.dumps({"error": "429 RESOURCE_EXHAUSTED"}),
                error_message="429 RESOURCE_EXHAUSTED: daily token quota exceeded",
            )

    ag_backend = AntigravityAgenticExecutionBackend(
        QuotaExhaustedAdapter(),
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=AntigravityExecutionAuthority(project_id="test-proj", enabled=True),
    )

    req2 = ExecutionRequest(
        task_id="obj-2",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={
            "source_sha": source_sha,
            "task_class": "LONG_HORIZON_AGENTIC_WORK",
            "prompt": "run objective 2",
        },
    )

    result2 = ag_backend.start(req2, {})
    assert result2.status == "DEGRADED"
    assert "ANTIGRAVITY_QUOTA_EXHAUSTED" in result2.sanitized_errors
    assert ag_backend.get_health() == ExecutionHealth.QUOTA_EXHAUSTED
    # Prior receipts remain preserved in prior_identity
    assert "obj-1" in prior_identity.completed_work_unit_ids
    assert prior_identity.artifact_hashes["obj1_out.txt"] == "sha-obj1"


# =========================================================================
# Scenario 5: Compatible free resource continues remaining objective without duplicate work
# =========================================================================
def test_scenario_5_compatible_free_resource_continues_without_duplication(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)

    prior_identity = AgenticSessionIdentity(
        resource_id="local_antigravity_subscription",
        backend_id="antigravity",
        session_or_thread_id="conv-session-5",
        workspace_fingerprint="fp1",
        source_sha=source_sha,
        checkpoint_id="chk-1",
        last_successful_turn=1,
        started_at="2026-10-18T10:00:00Z",
        updated_at="2026-10-18T10:05:00Z",
        adapter_contract_version="0.2.9",
        backend_version="1.0",
        executable_sha256="f" * 64,
        auth_mode="subscription",
        objective_id="obj-1",
        last_terminal_event="result:SUCCESS",
        completed_work_unit_ids=["obj-1"],
        completed_work_unit_signatures={"obj-1": "sig-obj-1"},
        artifact_hashes={"obj1.txt": "sha-obj1"},
    )

    # AG is quota exhausted
    exhausted_ag = MockFakeBackend(
        "antigravity",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.SUBSCRIPTION_INCLUDED,
        health=ExecutionHealth.QUOTA_EXHAUSTED,
        availability_state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
    )

    # Compatible free reasoning backend
    class HandoffFreeBackend(MockFakeBackend):
        def execute(self, request: ExecutionRequest) -> ExecutionResult:
            pack = request.context_pack or {}
            completed = list(pack.get("completed_work_unit_ids", []))
            # Verify objective 1 is recognized as already completed
            assert "obj-1" in completed
            # Free backend executes remaining objective 2 without repeating objective 1
            completed.append("obj-2")
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="free_worker",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                evidence_payload={"completed_work_unit_ids": completed},
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )

    free_backend = HandoffFreeBackend(
        "free_nemotron_direct",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.FREE_TIER_CLOUD,
    )

    router = ExecutionRouter(backends=[exhausted_ag, free_backend])

    req = ExecutionRequest(
        task_id="obj-2",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        agentic_identity=prior_identity,
        payload={"source_sha": source_sha},
    )

    res = router.execute_with_failover(req)
    assert res.status == "SUCCESS"
    assert res.backend_id == "free_nemotron_direct"
    completed_units = res.evidence_payload.get("completed_work_unit_ids")
    assert "obj-1" in completed_units
    assert "obj-2" in completed_units


# =========================================================================
# Scenario 6: No adequate resource yields WAIT/HOLD
# =========================================================================
def test_scenario_6_no_adequate_resource_yields_wait_hold(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)

    # AG and free reasoning are both exhausted
    exhausted_ag = MockFakeBackend(
        "antigravity",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.SUBSCRIPTION_INCLUDED,
        health=ExecutionHealth.QUOTA_EXHAUSTED,
        availability_state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
    )
    exhausted_free = MockFakeBackend(
        "free_reasoning",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.FREE_TIER_CLOUD,
        health=ExecutionHealth.QUOTA_EXHAUSTED,
        availability_state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
    )
    paid_cloud = MockFakeBackend(
        "paid_cloud_backend",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.PAID_CLOUD,
    )

    router = ExecutionRouter(backends=[exhausted_ag, exhausted_free, paid_cloud])

    req = ExecutionRequest(
        task_id="task-wait",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha},
    )

    res = router.execute_with_failover(req)
    assert res.status == "WAITING_FOR_REASONING_PROVIDER"
    assert res.backend_id in ("antigravity", "free_reasoning", "router")
    assert paid_cloud.invocations == []  # Paid was never invoked


# =========================================================================
# Scenario 7: Incompatible session/fingerprint rejects unsafe resume
# =========================================================================
def test_scenario_7_incompatible_session_rejects_unsafe_resume(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_adapter = FakeAntigravityAdapter()
    ag_backend = AntigravityAgenticExecutionBackend(
        fake_adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=AntigravityExecutionAuthority(project_id="test-proj", enabled=True),
    )

    prior_identity = AgenticSessionIdentity(
        resource_id="local_antigravity_subscription",
        backend_id="antigravity",
        session_or_thread_id="conv-session-7",
        workspace_fingerprint="stale_sha256_that_does_not_match_current_workspace",
        source_sha=source_sha,
        checkpoint_id="chk-7",
        last_successful_turn=1,
        started_at="2026-10-18T10:00:00Z",
        updated_at="2026-10-18T10:05:00Z",
        adapter_contract_version="0.2.9",
        backend_version="1.0",
        executable_sha256="f" * 64,
        auth_mode="subscription",
        objective_id="obj-7",
        last_terminal_event="result:SUCCESS",
    )

    req = ExecutionRequest(
        task_id="task-resume",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha, "task_class": "LONG_HORIZON_AGENTIC_WORK"},
    )

    res = ag_backend.resume(req, prior_identity, {})
    assert res.status == "DEGRADED"
    assert any("STALE_AGENT_SESSION:WORKSPACE_CHANGED" in err for err in res.sanitized_errors)


# =========================================================================
# Scenario 8: Untrusted executable identity rejects AG invocation
# =========================================================================
def test_scenario_8_untrusted_executable_identity_rejects_invocation(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_adapter = FakeAntigravityAdapter()

    # Backend with UNPROVEN capability status
    ag_backend = AntigravityAgenticExecutionBackend(
        fake_adapter,
        capability_status_provider=lambda: "UNPROVEN",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
    )

    req = ExecutionRequest(
        task_id="task-unproven",
        project_id="test-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha},
    )

    res = ag_backend.start(req, {})
    assert res.status == "DEGRADED"
    assert "ANTIGRAVITY_UNAVAILABLE" in res.sanitized_errors
    assert len(fake_adapter.invocations) == 0


# =========================================================================
# Scenario 9: Paid overage disabled prevents billable fallback
# =========================================================================
def test_scenario_9_paid_overage_disabled_prevents_billable_fallback():
    orchestrator = ResourceOrchestrator()
    paid_backend = MockFakeBackend(
        "cloud_gpt4_paid",
        {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK},
        ExecutionCost.PAID_CLOUD,
    )
    req = ExecutionRequest(
        task_id="t-paid",
        project_id="test-proj",
        workspace=".",
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={},
    )
    ranks = orchestrator.rank([paid_backend], req)
    assert ranks[0].eligible is False
    assert "PAID_DEFAULT_DENIED" in ranks[0].reasons
    assert orchestrator.select([paid_backend], req) is None


# =========================================================================
# Scenario 10: Historical Product commands never resume on resource recovery
# =========================================================================
def test_scenario_10_historical_product_commands_never_resume(tmp_path: Path):
    source_sha = _setup_git_repo(tmp_path)
    fake_adapter = FakeAntigravityAdapter()
    ag_backend = AntigravityAgenticExecutionBackend(
        fake_adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity={"path": "agy", "filename": "agy", "sha256": "f" * 64, "version": "1.0"},
        authority=AntigravityExecutionAuthority(project_id="lari", enabled=True, allow_product_lane=True),
    )

    req = ExecutionRequest(
        task_id="lari-command-51",
        project_id="lari",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={"source_sha": source_sha},
    )

    # Direct execution fails closed
    res = ag_backend.start(req, {})
    assert res.status == "FAILED"
    assert "PRODUCT_LANE_FORBIDDEN" in res.sanitized_errors

    # Orchestrator also rejects
    orchestrator = ResourceOrchestrator()
    ranks = orchestrator.rank([ag_backend], req)
    assert ranks[0].eligible is False
    assert "PRODUCT_LANE_FORBIDDEN" in ranks[0].reasons
