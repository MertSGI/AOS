from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest

from aos.design_render_entry import resolve_render_entry
from aos.platform_recovery import (
    PlatformRecoveryCoordinator,
    RepairDisposition,
    SourceRepairResourceUnavailable,
    SourceRepairResult,
)
from aos.recovery_proof import RecoveryProofStore
from aos.resource_snapshot import ResourceSnapshotStore
from aos.resource_ledger import ResourceEventType, ResourceLedger
from aos.source_repair_pipeline import (
    IsolatedSourceRepairPipeline,
    ResourceBackedRepairDriver,
)
from aos.autonomous_host import build_execution_router
from aos.controller_relay import ControllerRelayPublisher
from aos.runtime_admission import AdmissionState, CommandAdmissionStore
from aos.runtime_store import RuntimeStore
from aos.self_diagnosis import SelfDiagnosisEngine, ShadowRepairProposal
from aos.self_repair import (
    AUTHORITY_AUTO_REPAIR_ELIGIBLE,
    BoundedSelfRepairEngine,
    RepairActuationResult,
    ResourceReprobeActuator,
)
from aos.validate import validate_document
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


SOURCE_SHA = "5ce59e16be1490ac2c56fc92bdf9fcdfc16f1bec"


def _command(command_id: str, project_id: str = "lari") -> dict:
    return {
        "contract_version": "1.0.0",
        "command_id": command_id,
        "goal": "bounded recovery proof",
        "project": {"project_id": project_id},
    }


def _finding(diag: SelfDiagnosisEngine, *, requires_candidate: bool = False):
    return diag.record_or_update_finding(
        component="reasoning_provider",
        failure_class="PROVIDER_TRANSIENT_FAILURE",
        symptom="Bounded provider service recovery required",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=["lari"],
        evidence_refs=["provider-circuits.json"],
        evidence_class="LOCAL_RUNTIME_PROOF",
        confidence=0.95,
        suspected_root_cause="Local service stopped",
        repair_authority=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        requires_candidate=requires_candidate,
        proposed_repair=ShadowRepairProposal(
            problem="Local service stopped",
            evidence=["provider-circuits.json"],
            root_cause_hypothesis="Managed process exited",
            minimal_change="Restart owned local process and prove readiness",
            files_likely_affected=[],
            tests_required=["tests/test_autonomous_recovery_director.py"],
            ci_required=requires_candidate,
            runtime_proof_required="READINESS_PASS",
            rollback_plan="Stop owned process",
            authority_class=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        ),
    )


def test_command_admission_is_restart_persistent_and_legacy_missing_fails_closed(tmp_path):
    store = RuntimeStore(tmp_path)
    store.create_command(_command("continue-current"))
    first = CommandAdmissionStore(tmp_path)
    assert first.get("continue-current").state == "ACTIVE"
    persisted = first._load()
    assert validate_document("command_admission", persisted).is_valid

    first.set_state(
        "continue-current", AdmissionState.HOLD,
        authority="TEST", reason="BOUNDED_HOLD",
    )
    restarted = CommandAdmissionStore(tmp_path)
    assert restarted.get("continue-current").state == "HOLD"

    store.create_command(_command("continue-legacy"))
    document = restarted.snapshot()
    del document["records"]["continue-legacy"]
    from aos.runtime_store import atomic_json
    atomic_json(restarted.path, document)
    legacy = CommandAdmissionStore(tmp_path).get("continue-legacy")
    assert legacy.state == "HOLD"
    assert legacy.legacy_missing_record is True


def test_selected_resume_is_atomic_and_superseded_is_irreversible(tmp_path):
    store = RuntimeStore(tmp_path)
    for command_id in ("continue-a", "continue-b"):
        store.create_command(_command(command_id))
    admissions = CommandAdmissionStore(tmp_path)
    admissions.set_state("continue-a", "HOLD", authority="TEST", reason="WAIT")
    admissions.set_state("continue-b", "SUPERSEDED", authority="TEST", reason="REPLACED")

    with pytest.raises(ValueError, match="Superseded"):
        admissions.activate_many(
            ["continue-a", "continue-b"], authority="TEST", reason="SELECTED"
        )
    assert admissions.get("continue-a").state == "HOLD"
    assert admissions.get("continue-b").state == "SUPERSEDED"


def test_recovery_proof_rejects_wrong_binding_expiry_and_replay(tmp_path):
    store = RuntimeStore(tmp_path)
    store.create_command(_command("continue-terminal"))
    store.write_state(
        "continue-terminal",
        state="HUMAN_REQUIRED",
        canonical_source_sha=SOURCE_SHA,
    )
    proofs = RecoveryProofStore(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    proof = proofs.issue(
        command_id="continue-terminal",
        project_id="lari",
        canonical_revision=SOURCE_SHA,
        runtime_source_sha=SOURCE_SHA,
        root_cause="RECOVERY_CHURN_GUARD",
        evidence={"postcondition": "PASS", "candidate_sha": SOURCE_SHA},
        authority="CONTROLLER_REVIEW",
        expires_at=future,
        recovery_fingerprint="f" * 64,
    )
    assert validate_document("recovery_proof", proof).is_valid
    command = store.read_command("continue-terminal")
    state = store.read_state("continue-terminal")
    with pytest.raises(ValueError, match="project_id mismatch"):
        proofs.validate(
            proof["proof_id"],
            command={**command, "project": {"project_id": "other"}},
            state=state,
            canonical_revision=SOURCE_SHA,
            runtime_source_sha=SOURCE_SHA,
        )
    proofs.validate(
        proof["proof_id"], command=command, state=state,
        canonical_revision=SOURCE_SHA, runtime_source_sha=SOURCE_SHA,
    )
    proofs.accept(proof["proof_id"], accepted_by="controller")
    with pytest.raises(ValueError, match="consumed"):
        proofs.validate(
            proof["proof_id"], command=command, state=state,
            canonical_revision=SOURCE_SHA, runtime_source_sha=SOURCE_SHA,
        )

    expired = proofs.issue(
        command_id="continue-terminal",
        project_id="lari",
        canonical_revision=SOURCE_SHA,
        runtime_source_sha=SOURCE_SHA,
        root_cause="OLD_PROOF",
        evidence={"postcondition": "PASS"},
        authority="CONTROLLER_REVIEW",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(ValueError, match="expired"):
        proofs.validate(
            expired["proof_id"], command=command, state=state,
            canonical_revision=SOURCE_SHA, runtime_source_sha=SOURCE_SHA,
        )


def test_self_repair_requires_real_actuation_and_verified_postcondition(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag)
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)
    repair.set_live_mode(True)

    success, reason, record = repair.attempt_autonomous_repair(finding.finding_id)
    assert success is False
    assert reason == "FAILED_VERIFICATION"
    assert record["result"]["status"] == "NOT_APPLIED"
    assert diag.get_finding(finding.finding_id)["resolved_at"] is None

    repair.register_actuator(
        "PROVIDER_TRANSIENT_FAILURE",
        lambda _finding, _proposal: RepairActuationResult(
            operation_performed=True,
            postcondition_verified=True,
            operation="OWNED_PROCESS_RESTART",
            evidence={"readyz": "PASS", "pid_owned": True},
            rollback_reference="owned-process-stop",
        ),
    )
    success, _, record = repair.attempt_autonomous_repair(finding.finding_id)
    assert success is True
    assert record["result"]["operation_performed"] is True
    assert record["result"]["postcondition_verified"] is True
    assert BoundedSelfRepairEngine(tmp_path / "repair", diag).live_active is True


def test_self_repair_defaults_to_shadow_governed(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)

    assert repair.live_active is False
    assert repair.summarize_cockpit()["self_repair_mode"] == "SHADOW_GOVERNED"


def test_resource_reprobe_actuator_requires_observed_postcondition():
    class Client:
        def __init__(self):
            self.calls = 0

        def status(self):
            self.calls += 1
            return {
                "provider_probe_count": 1 if self.calls == 1 else 2,
                "healthy_reasoning_providers": [] if self.calls == 1 else ["local"],
            }

        def reprobe_resources(self):
            return {"status": "RESOURCE_REPROBE_COMPLETED"}

    result = ResourceReprobeActuator(Client())(
        {"finding_id": "finding-1"}, {"minimal_change": "reprobe"}
    )
    assert result.operation_performed is True
    assert result.postcondition_verified is True
    assert result.evidence["healthy_provider_count"] == 1


def test_platform_recovery_source_pipeline_stops_at_promotion_ready(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=True)
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)
    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        repair,
        source_base_sha=SOURCE_SHA,
        source_repair_executor=lambda request: SourceRepairResult(
            isolated_worktree=str(tmp_path / "isolated"),
            branch="fix/system-repair-job",
            base_sha=request["required_base_sha"],
            repair_sha="a" * 40,
            workspace_fingerprint="workspace-fingerprint",
            resource_backend_id="native_execution",
            attempt_telemetry={"attempt_count": 1},
            git_diff_sha256="d" * 64,
            candidate_manifest={"source_sha": "a" * 40},
            rollback_information={"base_sha": SOURCE_SHA},
            tests_passed=True,
            evidence_valid=True,
            exact_sha_ci_status="SUCCESS",
            candidate_materialized=True,
            promotion_performed=False,
            activation_performed=False,
            evidence={"ci_run_id": "synthetic-test"},
        ),
    )
    job = coordinator.process_finding(finding.finding_id)
    assert job["job_type"] == "SYSTEM_REPAIR_JOB"
    assert job["product_command_id"] is None
    assert job["disposition"] == RepairDisposition.PROMOTION_READY.value
    assert job["activation_permitted"] is False
    assert job["promotion_permitted"] is False
    assert validate_document("system_repair_job", job).is_valid


def test_platform_recovery_resource_unavailable_is_a_typed_wait(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=True)
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)

    def unavailable(_request):
        raise SourceRepairResourceUnavailable("NO_ELIGIBLE_SOURCE_REPAIR_RESOURCE")

    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        repair,
        source_base_sha=SOURCE_SHA,
        source_repair_executor=unavailable,
    )
    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.WAITING_FOR_RESOURCE.value
    assert job["blocker"] == "SOURCE_REPAIR_RESOURCE_UNAVAILABLE"
    assert job["error_class"] == "SourceRepairResourceUnavailable"


def test_platform_recovery_missing_source_executor_waits_without_fallback(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=True)
    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        BoundedSelfRepairEngine(tmp_path / "repair", diag),
        source_base_sha=SOURCE_SHA,
    )

    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.WAITING_FOR_RESOURCE.value
    assert job["blocker"] == "SOURCE_REPAIR_EXECUTOR_UNAVAILABLE"


@pytest.mark.parametrize(
    "override",
    [
        {"base_sha": "b" * 40},
        {"repair_sha": SOURCE_SHA, "candidate_manifest": {"source_sha": SOURCE_SHA}},
        {"exact_sha_ci_status": "IN_PROGRESS"},
        {"candidate_materialized": False},
        {"promotion_performed": True},
        {"activation_performed": True},
    ],
)
def test_platform_recovery_rejects_unproven_source_results(tmp_path, override):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=True)
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)

    def executor(request):
        repair_sha = str(override.get("repair_sha") or "a" * 40)
        values = {
            "isolated_worktree": str(tmp_path / "isolated"),
            "branch": "repair/aos-system-test",
            "base_sha": request["required_base_sha"],
            "repair_sha": repair_sha,
            "workspace_fingerprint": "workspace-fingerprint",
            "resource_backend_id": "codex_cli",
            "attempt_telemetry": {"attempt_count": 1},
            "git_diff_sha256": "d" * 64,
            "candidate_manifest": {"source_sha": repair_sha},
            "rollback_information": {"base_sha": SOURCE_SHA},
            "tests_passed": True,
            "evidence_valid": True,
            "exact_sha_ci_status": "SUCCESS",
            "candidate_materialized": True,
            "promotion_performed": False,
            "activation_performed": False,
        }
        values.update(override)
        return SourceRepairResult(**values)

    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        repair,
        source_base_sha=SOURCE_SHA,
        source_repair_executor=executor,
    )
    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.FAILED_VERIFICATION.value


def test_platform_recovery_never_dispatches_unsafe_source_finding(tmp_path):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = diag.record_or_update_finding(
        component="production",
        failure_class="UNKNOWN",
        symptom="Production credential policy change requested",
        severity="HIGH",
        autonomy_impact="BLOCKED",
        affected_lane_ids=[],
        evidence_refs=["finding"],
        evidence_class="SOURCE_PROOF",
        confidence=1.0,
        suspected_root_cause="Policy boundary",
        repair_authority="FORBIDDEN",
        requires_candidate=True,
    )
    called = False

    def executor(_request):
        nonlocal called
        called = True
        raise AssertionError("unsafe finding must not dispatch")

    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        BoundedSelfRepairEngine(tmp_path / "repair", diag),
        source_base_sha=SOURCE_SHA,
        source_repair_executor=executor,
    )
    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.UNSAFE_TO_REPAIR.value
    assert called is False


def test_resource_backed_repair_driver_uses_router_selection_not_a_named_worker(tmp_path):
    class Router:
        last_attempt_telemetry = [
            {"backend_id": "codex_cli", "status": "SUCCESS", "final_selection": True}
        ]

        def execute_with_failover(self, request):
            assert request.payload["resource_requirements"]["scarcity_policy"] == "AVOID_SCARCE"
            assert ExecutionCapability.LONG_HORIZON_AGENTIC_WORK in request.required_capabilities
            assert request.write_scope == ["src/aos/platform_recovery.py"]
            return ExecutionResult(
                backend_id="codex_cli",
                worker_id="codex_cli",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                changed_paths=["src/aos/platform_recovery.py"],
            )

    driver = ResourceBackedRepairDriver(Router())
    evidence = driver({
        "job_id": "repair-resource-selection",
        "required_base_sha": SOURCE_SHA,
        "finding": {
            "repair_authority": AUTHORITY_AUTO_REPAIR_ELIGIBLE,
            "proposed_repair": {
                "files_likely_affected": ["src/aos/platform_recovery.py"]
            },
        },
    }, tmp_path)

    assert evidence["resource_backend_id"] == "codex_cli"
    assert evidence["attempt_telemetry"]["selected_backend_id"] == "codex_cli"


def test_resource_snapshot_preserves_unknown_and_render_contract_is_project_aware(tmp_path):
    class Backend:
        backend_id = "local-tool"
        backend_class = "NATIVE_EXECUTION_BACKEND"
        cost = ExecutionCost.FREE_LOCAL
        supported_capabilities = {ExecutionCapability.FILE_READ}

        def get_health(self):
            return ExecutionHealth.HEALTHY

    ledger = ResourceLedger(tmp_path / "resource-ledger.jsonl")
    snapshots = ResourceSnapshotStore(
        tmp_path / "resource-snapshot.json", ledger=ledger
    )
    observed = snapshots.observe_and_record([Backend()])["local-tool"]
    assert observed.health == "HEALTHY"
    assert observed.credential_status == "UNKNOWN"
    assert observed.local_service_status == "UNKNOWN"
    assert observed.quota_state == "UNKNOWN"
    request = ExecutionRequest(
        task_id="task-1", project_id="lari", workspace=str(tmp_path),
        operation_class="read", required_capabilities=[ExecutionCapability.FILE_READ],
        authority_id="test",
    )
    rank = ResourceOrchestrator(snapshot_store=snapshots).rank([Backend()], request)
    assert rank[0].eligible is True
    assert snapshots.read()["resources"]["local-tool"]["quota_state"] == "UNKNOWN"
    event = ledger.events([ResourceEventType.RESOURCE_SNAPSHOT.value])[-1]
    assert event.payload["resource_id"] == "local-tool"
    assert event.payload["credential_status"] == "UNKNOWN"

    workspace = tmp_path / "workspace"
    entry = workspace / "apps" / "web" / "home.html"
    css = workspace / "apps" / "web" / "theme.css"
    entry.parent.mkdir(parents=True)
    (workspace / ".aos").mkdir()
    entry.write_text("<html></html>", encoding="utf-8")
    css.write_text("body{}", encoding="utf-8")
    (workspace / ".aos" / "render-entry.json").write_text(
        '{"schema_version":"1.0.0","render_type":"STATIC_HTML",'
        '"entrypoint":"apps/web/home.html","stylesheet":"apps/web/theme.css"}',
        encoding="utf-8",
    )
    resolved = resolve_render_entry(workspace)
    assert resolved.entrypoint == entry.resolve()
    assert resolved.stylesheet == css.resolve()
    assert resolved.source == "PROJECT_CONTRACT"


def test_execution_router_wires_one_resource_truth_plane(tmp_path):
    policy = Path("descriptors/nemotron.planner-policy.json").resolve()
    router = build_execution_router(policy, tmp_path / "runtime")
    provider = router.get_backend("provider_failover_reasoning_backend")
    assert provider is not None
    assert router.orchestrator.snapshot_store is not None
    assert router.orchestrator.snapshot_store.ledger is provider.resource_ledger
    assert provider.quota_governor.ledger is provider.resource_ledger


def test_controller_relay_persists_platform_job_without_actuation(tmp_path):
    relay = ControllerRelayPublisher(
        tmp_path / "relay",
        {
            "runtime_root": str(tmp_path / "state"),
            "candidate_source_sha": SOURCE_SHA,
        },
    )
    finding = _finding(relay.diagnostics)
    relay.diagnostics.diagnose_runtime = lambda **_kwargs: [finding]
    relay.collect_snapshot(force_diagnosis=True)
    jobs = relay.platform_recovery.list_recent_jobs()
    assert len(jobs) == 1
    assert jobs[0]["finding_id"] == finding.finding_id
    assert jobs[0]["disposition"] == RepairDisposition.WAITING_FOR_RESOURCE.value
    assert jobs[0]["execution_started"] is False


def test_controller_relay_wires_real_isolated_source_repair_executor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            capture_output=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "AOS Test")
    git("config", "user.email", "aos-test@example.invalid")
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    def repair_driver(_request, workspace):
        (workspace / "source.txt").write_text("repaired\n", encoding="utf-8")
        return {
            "resource_backend_id": "codex_cli",
            "attempt_telemetry": {"attempt_count": 1},
        }

    pipeline = IsolatedSourceRepairPipeline(
        repo,
        tmp_path / "worktrees",
        repair_driver=repair_driver,
        publisher=lambda _workspace, _branch, sha: {
            "remote_sha": sha, "push_mode": "NORMAL"
        },
        certifier=lambda _workspace, _branch, sha, _publication: {
            "candidate_manifest": {"source_sha": sha, "immutable": True},
            "tests_passed": True,
            "evidence_valid": True,
            "exact_sha_ci_status": "SUCCESS",
            "candidate_materialized": True,
        },
    )
    relay = ControllerRelayPublisher(
        tmp_path / "relay",
        {"runtime_root": str(tmp_path / "state"), "candidate_source_sha": base_sha},
        source_repair_executor=pipeline,
    )
    finding = _finding(relay.diagnostics, requires_candidate=True)
    job = relay.platform_recovery.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.PROMOTION_READY.value
    assert job["source_repair"]["base_sha"] == base_sha
    assert job["source_repair"]["repair_sha"] != base_sha
    assert job["source_repair"]["promotion_performed"] is False
    assert job["source_repair"]["activation_performed"] is False


def test_isolated_source_repair_pipeline_proves_lineage_and_stops_before_promotion(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            capture_output=True,
        ).stdout.strip()

    git("init")
    git("config", "user.name", "AOS Test")
    git("config", "user.email", "aos-test@example.invalid")
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    git("add", "source.txt")
    git("commit", "-m", "base")
    base_sha = git("rev-parse", "HEAD")

    def repair_driver(_request, workspace):
        (workspace / "source.txt").write_text("repaired\n", encoding="utf-8")
        return {
            "resource_backend_id": "native_execution",
            "attempt_telemetry": {"attempt_count": 1},
        }

    def publisher(_workspace, _branch, repair_sha):
        return {"remote_sha": repair_sha, "push_mode": "FAST_FORWARD_ONLY"}

    def certifier(_workspace, _branch, repair_sha, _publication):
        return {
            "candidate_manifest": {"source_sha": repair_sha},
            "tests_passed": True,
            "evidence_valid": True,
            "exact_sha_ci_status": "SUCCESS",
            "candidate_materialized": True,
            "evidence": {"ci_run_id": "synthetic"},
        }

    pipeline = IsolatedSourceRepairPipeline(
        repo,
        tmp_path / "worktrees",
        repair_driver=repair_driver,
        publisher=publisher,
        certifier=certifier,
    )
    result = pipeline({
        "job_id": "repair-test-1",
        "required_base_sha": base_sha,
        "requirements": {
            "isolated_worktree": True,
            "exact_sha_ci": True,
            "immutable_candidate": True,
            "promotion": False,
            "activation": False,
        },
    })
    assert result.base_sha == base_sha
    assert result.repair_sha != base_sha
    assert result.candidate_manifest["source_sha"] == result.repair_sha
    assert git("rev-parse", f"{result.repair_sha}^") == base_sha
    assert result.promotion_performed is False
    assert result.activation_performed is False
