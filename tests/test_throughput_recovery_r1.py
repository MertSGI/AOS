"""Deterministic throughput-recovery routing and admission regressions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aos.planning_kernel import (
    AuthorityRecord,
    PlanningKernelError,
    ProjectSituation,
    WaitingForReasoningProvider,
    _reason,
)
from aos.runtime_admission import AdmissionState
from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_server import RuntimeEngine
from extensions.autonomy_fabric.agentic_planning_bridge import (
    AgenticStructuredPlanningBridge,
)
from extensions.autonomy_fabric.cline_agentic_backend import (
    ClineAgenticExecutionBackend,
)
from extensions.autonomy_fabric.execution_backend import (
    AgenticExecutionBackend,
    AgenticSessionIdentity,
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}
CLINE_IDENTITY = {
    "path": "cline-test-double",
    "filename": "cline.cmd",
    "sha256": "f" * 64,
    "version": "3.0.65",
}


class _OrderedOrchestrator:
    def __init__(self, order):
        self.order = list(order)

    def select(self, backends, request):
        available = {backend.backend_id for backend in backends}
        return next((item for item in self.order if item in available), None)


class _ReasoningBackend(ExecutionBackend):
    backend_class = BackendClass.REASONING_BACKEND
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.MODEL_REASONING}
    quality_tier = 3
    context_window_tokens = 128_000
    expected_latency_ms = 5

    def __init__(
        self,
        backend_id: str,
        *,
        proposal=None,
        failure_class: str | None = None,
        availability_state: ExecutionAvailabilityState | None = None,
        cost: ExecutionCost = ExecutionCost.FREE_TIER_CLOUD,
    ):
        self.backend_id = backend_id
        self.proposal = proposal
        self.failure_class = failure_class
        self.availability_state = availability_state
        self.cost = cost
        self.calls: list[ExecutionRequest] = []

    def get_health(self):
        return ExecutionHealth.HEALTHY

    def get_availability(self):
        return ExecutionAvailabilitySnapshot(
            ExecutionAvailabilityState.AVAILABLE,
            "2026-09-30T00:00:00Z",
            source="TEST_DOUBLE",
        )

    def execute(self, request):
        self.calls.append(request)
        if self.failure_class is None:
            return ExecutionResult(
                backend_id=self.backend_id,
                worker_id="test-reasoner",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                evidence_payload={"proposal": dict(self.proposal or {"answer": "ok"})},
                evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            )
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="test-reasoner",
            task_id=request.task_id,
            request_id=request.request_id,
            status="DEGRADED",
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=[self.failure_class],
            evidence_payload={"failure_class": self.failure_class},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            availability=ExecutionAvailabilitySnapshot(
                self.availability_state or ExecutionAvailabilityState.CONTRACT_FAILURE,
                "2026-09-30T00:00:00Z",
                source="TEST_DOUBLE",
            ),
        )


class _AgenticPlanner(AgenticExecutionBackend):
    backend_class = BackendClass.AGENTIC_EXECUTION_BACKEND
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.LONG_HORIZON_AGENTIC_WORK}
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED

    def __init__(self, backend_id="alternate_agentic"):
        self.backend_id = backend_id
        self.calls = 0

    def get_health(self):
        return ExecutionHealth.HEALTHY

    def get_availability(self):
        return ExecutionAvailabilitySnapshot(
            ExecutionAvailabilityState.AVAILABLE,
            "2026-09-30T00:00:00Z",
            source="TEST_DOUBLE",
        )

    def start(self, request, context_pack):
        self.calls += 1
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="test-agentic",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            stdout_digest=json.dumps({"answer": "agentic"}),
            evidence_payload={"raw_output": json.dumps({"answer": "agentic"})},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )

    def resume(self, request, identity, context_pack):
        return self.start(request, context_pack)

    def interrupt(self, execution_id):
        return None


def _cline_bridge(tmp_path: Path) -> AgenticStructuredPlanningBridge:
    def must_not_launch(*_args, **_kwargs):
        raise AssertionError("Cline must fail at the prelaunch contract gate")

    cline = ClineAgenticExecutionBackend(
        runner=must_not_launch,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity=CLINE_IDENTITY,
        data_dir=str(tmp_path / "cline-data"),
        config_dir=str(tmp_path / "cline-config"),
    )
    return AgenticStructuredPlanningBridge(cline)


def _situation(tmp_path: Path, project_id: str = "lari") -> ProjectSituation:
    authority = AuthorityRecord(
        authority_id="DECISION-020",
        source_path="docs/project-control/DECISIONS.md",
        text="Routine non-production source work is authorized.",
        superseded=False,
        production_allowed=False,
    )
    return ProjectSituation(
        schema_version="1.0.0",
        project_id=project_id,
        repository=str(tmp_path),
        control_ref="control/project",
        control_sha="a" * 40,
        repository_head="b" * 40,
        execution_base_sha="c" * 40,
        current_status="ACTIVE",
        current_milestone="Throughput recovery",
        canonical_next_action="Continue bounded work",
        canonical_hashes={},
        canonical_excerpt="Routine non-production source work is authorized.",
        working_tree_state="CLEAN",
        ci_state=[],
        accepted_gates=[],
        blocked_gates=[],
        authority_records={"DECISION-020": authority},
        goal="Continue protected product lineage.",
        constraints=(),
        red_lines=("production activation",),
        completion_criteria=("bounded work complete",),
        ambiguity_reasons=(),
        captured_at="2026-09-30T00:00:00Z",
    )


def _planning_request(tmp_path: Path, *, task_id="plan") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        project_id="lari",
        workspace=str(tmp_path),
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="DECISION-020",
        request_id=f"request-{task_id}",
        payload={"prompt": "Return a plan", "schema": SCHEMA, "source_sha": "a" * 40},
    )


def test_cline_prelaunch_contract_failure_routes_to_direct_reasoner(tmp_path):
    cline = _cline_bridge(tmp_path)
    direct = _ReasoningBackend("direct_reasoner", proposal={"answer": "direct"})
    router = ExecutionRouter(
        [cline, direct],
        orchestrator=_OrderedOrchestrator([cline.backend_id, direct.backend_id]),
    )

    proposal = _reason(
        _situation(tmp_path),
        tmp_path / "policy.json",
        tmp_path / "runtime",
        "plan-a",
        "Return a plan",
        SCHEMA,
        "DECISION-020",
        backend_override=router,
        workspace=tmp_path,
    )

    assert proposal == {"answer": "direct"}
    assert len(direct.calls) == 1
    assert [row["backend_id"] for row in router.last_attempt_telemetry if "underlying" not in row["attempt_id"]] == [
        cline.backend_id,
        direct.backend_id,
    ]


def test_cline_prelaunch_contract_failure_routes_to_alternate_agentic_planner(tmp_path):
    cline = _cline_bridge(tmp_path)
    alternate = _AgenticPlanner()
    alternate_bridge = AgenticStructuredPlanningBridge(alternate)
    router = ExecutionRouter(
        [cline, alternate_bridge],
        orchestrator=_OrderedOrchestrator([cline.backend_id, alternate_bridge.backend_id]),
    )

    result = router.execute_with_failover(_planning_request(tmp_path, task_id="plan-b"))

    assert result.status == "SUCCESS"
    assert result.evidence_payload["proposal"] == {"answer": "agentic"}
    assert alternate.calls == 1


def test_later_than_third_eligible_backend_succeeds_once_in_ranked_order(tmp_path):
    backends = [
        _ReasoningBackend(
            f"degraded-{index}",
            failure_class=f"BACKEND_{index}_CONTRACT_FAILURE",
            availability_state=ExecutionAvailabilityState.CONTRACT_FAILURE,
        )
        for index in range(1, 5)
    ]
    success = _ReasoningBackend("success-5", proposal={"answer": "fifth"})
    router = ExecutionRouter(
        [*backends, success],
        orchestrator=_OrderedOrchestrator([item.backend_id for item in [*backends, success]]),
    )

    result = router.execute_with_failover(_planning_request(tmp_path, task_id="plan-c"))

    assert result.status == "SUCCESS"
    assert result.backend_id == "success-5"
    assert [len(item.calls) for item in [*backends, success]] == [1, 1, 1, 1, 1]


def test_all_genuine_resource_failures_enter_provider_wait(tmp_path):
    unavailable = [
        _ReasoningBackend(
            "quota",
            failure_class="QUOTA_EXHAUSTED",
            availability_state=ExecutionAvailabilityState.QUOTA_EXHAUSTED,
        ),
        _ReasoningBackend(
            "network",
            failure_class="NETWORK_UNAVAILABLE",
            availability_state=ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
        ),
    ]
    router = ExecutionRouter(
        unavailable,
        orchestrator=_OrderedOrchestrator([item.backend_id for item in unavailable]),
    )

    with pytest.raises(WaitingForReasoningProvider):
        _reason(
            _situation(tmp_path),
            tmp_path / "policy.json",
            tmp_path / "runtime",
            "plan-d",
            "Return a plan",
            SCHEMA,
            "DECISION-020",
            backend_override=router,
            workspace=tmp_path,
        )
    assert [len(item.calls) for item in unavailable] == [1, 1]


def test_backend_local_contract_failure_is_not_provider_outage(tmp_path):
    local_failure = _ReasoningBackend(
        "cline_planning_bridge",
        failure_class="CLINE_PRELAUNCH_CONTRACT_FAILURE",
        availability_state=ExecutionAvailabilityState.CONTRACT_FAILURE,
    )
    router = ExecutionRouter(
        [local_failure],
        orchestrator=_OrderedOrchestrator([local_failure.backend_id]),
    )

    with pytest.raises(PlanningKernelError, match="CLINE_PRELAUNCH_CONTRACT_FAILURE") as raised:
        _reason(
            _situation(tmp_path),
            tmp_path / "policy.json",
            tmp_path / "runtime",
            "plan-e",
            "Return a plan",
            SCHEMA,
            "DECISION-020",
            backend_override=router,
            workspace=tmp_path,
        )
    assert not isinstance(raised.value, WaitingForReasoningProvider)


@pytest.mark.parametrize(
    "lineage_id",
    ["continue-b181ddc574c25c2aa0f2a6b9", "continue-61be4ab1af53cfa646d773ce"],
)
def test_protected_lineage_and_completed_work_survive_backend_handoff(tmp_path, lineage_id):
    cline = _cline_bridge(tmp_path)
    direct = _ReasoningBackend("direct_reasoner", proposal={"answer": "continued"})
    router = ExecutionRouter(
        [cline, direct],
        orchestrator=_OrderedOrchestrator([cline.backend_id, direct.backend_id]),
    )
    request = _planning_request(tmp_path, task_id=lineage_id)
    request.agentic_identity = AgenticSessionIdentity(
        resource_id="cline-resource",
        backend_id="cline",
        workspace_fingerprint="d" * 64,
        source_sha="a" * 40,
        checkpoint_id="checkpoint-7",
        session_or_thread_id="cline-session-7",
        completed_work_unit_ids=["completed-1"],
        completed_work_unit_signatures={"completed-1": "e" * 64},
        artifact_hashes={"proof": "f" * 64},
    )

    result = router.execute_with_failover(request)

    assert result.status == "SUCCESS"
    assert direct.calls[0].task_id == lineage_id
    assert direct.calls[0].request_id == f"request-{lineage_id}"
    assert direct.calls[0].context_pack["completed_work_unit_ids"] == ["completed-1"]
    assert direct.calls[0].context_pack["completed_work_unit_signatures"] == {"completed-1": "e" * 64}
    assert direct.calls[0].context_pack["artifact_hashes"] == {"proof": "f" * 64}
    assert request.agentic_identity.completed_work_unit_ids == ["completed-1"]


def _runtime_config(tmp_path: Path):
    descriptor = tmp_path / "maintenance-descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "maintenance-workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "aos-maintenance": {
                "project_id": "aos-maintenance",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "aos-maintenance",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def _stop_threads(engine: RuntimeEngine):
    engine.stop_event.set()
    engine.recovery_thread.join(timeout=3.0)
    if engine.probe_thread is not None:
        engine.probe_thread.join(timeout=3.0)
    engine.telemetry_thread.join(timeout=3.0)


@pytest.mark.parametrize("admission", [AdmissionState.HOLD, AdmissionState.SUPERSEDED])
def test_held_or_superseded_maintenance_command_is_not_recovered(tmp_path, monkeypatch, admission):
    engine = RuntimeEngine(_runtime_config(tmp_path))
    try:
        _stop_threads(engine)
        profile = ProjectProfile(
            project_id="aos-maintenance",
            descriptor_path=str(tmp_path / "maintenance-descriptor.json"),
            workspace=str(tmp_path / "maintenance-workspace"),
            routing_policy_path=str(tmp_path / "policy.json"),
        )
        command = ContinueProjectCommand.from_mapping({"goal": "bounded canary"}, project=profile)
        engine.store.create_command(command.to_dict())
        engine.store.write_state(command.command_id, state="RUNNING", worker_pid=None)
        engine.admissions.set_state(
            command.command_id,
            AdmissionState.ACTIVE,
            authority="TEST",
            reason="TEST_SETUP",
        )
        if admission == AdmissionState.HOLD:
            engine.hold_command(command.command_id, authority="TEST", reason="CANARY_COMPLETE")
        else:
            engine.supersede_command(command.command_id, authority="TEST", reason="CANARY_COMPLETE")

        spawned = []
        monkeypatch.setattr(
            engine,
            "_spawn_worker",
            lambda command_id, recovered: spawned.append((command_id, recovered)) or 123,
        )
        engine._recover_one(command.command_id)

        assert spawned == []
        assert engine.admissions.get(command.command_id).state == admission.value
        assert (engine.store.command_dir(command.command_id) / "command.json").is_file()
        assert engine.store.read_command(command.command_id)["command_id"] == command.command_id
    finally:
        engine.shutdown()


def test_paid_backend_and_production_operation_remain_denied(tmp_path):
    paid = _ReasoningBackend("paid", cost=ExecutionCost.PAID_CLOUD)
    router = ExecutionRouter([paid])
    result = router.execute_with_failover(_planning_request(tmp_path, task_id="paid-denied"))
    assert result.status in {"FAILED", "DENIED"}
    assert paid.calls == []

    production = _planning_request(tmp_path, task_id="production-denied")
    production.operation_class = "PRODUCTION_RELEASE"
    assert router.select_backend(production) is None
    assert paid.calls == []


def test_maintenance_descriptor_is_preserved():
    descriptor = Path("descriptors/aos-maintenance.autonomous-host.descriptor.json")
    assert descriptor.is_file()
    assert json.loads(descriptor.read_text(encoding="utf-8"))["project_id"] == "aos-maintenance"
