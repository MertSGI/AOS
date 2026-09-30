from __future__ import annotations

import json
from pathlib import Path

import pytest

import aos.runtime_deploy as runtime_deploy
from aos.controller_relay import ControllerRelayPublisher
from aos.knowledge.accepted_work import (
    AcceptedWorkCoverageError,
    accepted_work_coverage,
    assert_accepted_work_receipted,
)
from aos.knowledge.hooks import resolve_knowledge_ledger
from aos.knowledge.ingress import ingest_accepted_work
from aos.knowledge.__main__ import main as knowledge_main
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.model import AuthorityClass, KnowledgeEventType
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt
from aos.platform_recovery import PlatformRecoveryCoordinator, RepairDisposition, SourceRepairResult
from aos.self_diagnosis import SelfDiagnosisEngine, ShadowRepairProposal
from aos.self_repair import (
    AUTHORITY_AUTO_REPAIR_ELIGIBLE,
    BoundedSelfRepairEngine,
    RepairActuationResult,
)


BASE_SHA = "a" * 40
RESULT_SHA = "b" * 40
OTHER_SHA = "c" * 40


def _ingest(ledger: KnowledgeLedger, agent_class: str = "CODEX", **overrides):
    values = {
        "project_id": "AOS",
        "agent_class": agent_class,
        "tool_name": f"{agent_class.lower()}-cli",
        "base_sha": BASE_SHA,
        "result_sha": RESULT_SHA,
        "repository": "MertSGI/AOS",
        "branch": "fix/kcp-test",
        "module_ids": ["KCP"],
        "changed_paths": ["src/aos/knowledge/accepted_work.py"],
        "evidence_refs": ["ci-run:123"],
        "verification_status": "SUCCESS",
        "canonical_next_action": "Review exact-SHA candidate",
        "idempotency_key": f"test-ingress-{agent_class.lower()}",
        "preflight_hash": "d" * 64,
    }
    values.update(overrides)
    return ingest_accepted_work(ledger, **values)


def _finding(diag: SelfDiagnosisEngine, *, requires_candidate: bool):
    return diag.record_or_update_finding(
        component="reasoning_provider",
        failure_class="PROVIDER_TRANSIENT_FAILURE",
        symptom="bounded recovery required",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=["lari"],
        evidence_refs=["provider-circuits.json"],
        evidence_class="LOCAL_RUNTIME_PROOF",
        confidence=0.95,
        suspected_root_cause="owned local service stopped",
        repair_authority=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        requires_candidate=requires_candidate,
        proposed_repair=ShadowRepairProposal(
            problem="owned local service stopped",
            evidence=["provider-circuits.json"],
            root_cause_hypothesis="managed process exited",
            minimal_change="restart or repair bounded component",
            files_likely_affected=["src/aos/platform_recovery.py"] if requires_candidate else [],
            tests_required=["tests/test_kcp_enforcement.py"],
            ci_required=requires_candidate,
            runtime_proof_required="READINESS_PASS",
            rollback_plan="restore prior state",
            authority_class=AUTHORITY_AUTO_REPAIR_ELIGIBLE,
        ),
    )


def test_runtime_config_path_resolves_runtime_v1_ledger(tmp_path: Path):
    runtime_home = tmp_path / "runtime-v1"
    config_path = runtime_home / "runtime-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"runtime_root": str(runtime_home / "state")}), "utf-8")

    ledger = resolve_knowledge_ledger(config={"runtime_config_path": str(config_path)})

    assert ledger is not None
    assert ledger.root == (runtime_home / "knowledge").resolve()


def test_controller_relay_platform_recovery_receives_runtime_ledger(tmp_path: Path):
    runtime_home = tmp_path / "runtime-v1"
    config_path = runtime_home / "runtime-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"runtime_root": str(runtime_home / "state")}), "utf-8")

    relay = ControllerRelayPublisher(
        tmp_path / "relay",
        {"runtime_config_path": str(config_path), "candidate_source_sha": BASE_SHA},
    )

    assert relay.platform_recovery.knowledge_ledger is not None
    assert relay.platform_recovery.knowledge_ledger.root == (runtime_home / "knowledge").resolve()


@pytest.mark.parametrize("agent_class", ["CODEX", "CLINE", "CONTROLLER"])
def test_external_agent_ingress_writes_accepted_coverage(tmp_path: Path, agent_class: str):
    ledger = KnowledgeLedger(tmp_path / "knowledge")

    result = _ingest(ledger, agent_class)

    assert result["status"] == "ACCEPTED_WORK_RECEIPTED"
    assert result["archival_status"] == "KCP_ARCHIVED"
    assert result["implementation_receipt_id"]
    assert result["verification_receipt_id"]
    assert result["KCP_ARCHIVAL_STATUS"] == "KCP_ARCHIVED"
    assert result["KCP_RESULT_SHA"] == RESULT_SHA
    assert result["KCP_PREFLIGHT_HASH"] == "d" * 64
    coverage = accepted_work_coverage(ledger, project_id="AOS", result_sha=RESULT_SHA)
    assert coverage["covered"] is True
    assert result["ledger_sequence"] == coverage["ledger_sequence"]


def test_external_ingress_rejects_invalid_agent_and_missing_result_sha(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    with pytest.raises(ValueError):
        _ingest(ledger, "UNREGISTERED_AGENT")
    with pytest.raises(ValueError, match="result SHA"):
        _ingest(ledger, result_sha="")
    assert ledger.read_events() == ()


def test_failed_verification_cannot_create_accepted_coverage(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")

    with pytest.raises(ValueError, match="not acceptable"):
        _ingest(ledger, verification_status="FAILED")

    assert ledger.read_events() == ()
    assert accepted_work_coverage(ledger, project_id="AOS", result_sha=RESULT_SHA)["covered"] is False


def test_coverage_is_exact_sha_and_scope_bound(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    _ingest(ledger)

    assert accepted_work_coverage(
        ledger,
        project_id="AOS",
        result_sha=RESULT_SHA,
        module_ids=["KCP"],
        changed_paths=["src/aos/knowledge/accepted_work.py"],
    )["covered"] is True
    assert accepted_work_coverage(ledger, project_id="AOS", result_sha=OTHER_SHA)["covered"] is False
    with pytest.raises(AcceptedWorkCoverageError):
        assert_accepted_work_receipted(ledger, project_id="AOS", result_sha=OTHER_SHA)


def test_handoff_and_historical_current_truth_do_not_cover_accepted_work(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    ledger.append(
        KnowledgeEventType.HANDOFF,
        project_id="AOS",
        idempotency_key="handoff",
        authority_class=AuthorityClass.SECOND_BRAIN_MIRROR,
        agent_class="CODEX",
        tool_name="notebook",
        result_sha=RESULT_SHA,
    )
    ledger.append(
        KnowledgeEventType.CURRENT_TRUTH_OBSERVATION,
        project_id="AOS",
        idempotency_key="truth",
        authority_class=AuthorityClass.OPERATIONAL_TRUTH,
        agent_class="AOS_NATIVE",
        tool_name="aos.current_truth",
        result_sha=RESULT_SHA,
        current_truth_observed_at="2026-09-30T00:00:00Z",
        current_truth_status="KNOWN",
    )

    assert accepted_work_coverage(ledger, project_id="AOS", result_sha=RESULT_SHA)["covered"] is False


def test_stage_rejects_unreceipted_sha_and_explicit_bootstrap_enables_once(
    tmp_path: Path, monkeypatch
):
    runtime_home = tmp_path / "runtime-v1"
    loaded = []

    class Materializer:
        @staticmethod
        def materialize(source_sha, _ci_run_id, **_kwargs):
            candidate = runtime_home / "candidate" / source_sha
            candidate.mkdir(parents=True)
            return candidate

    monkeypatch.setattr(runtime_deploy, "_load_materializer", lambda _root: loaded.append(True) or Materializer)
    monkeypatch.setattr(
        runtime_deploy,
        "validate",
        lambda _home, source_sha: {
            "validation": "PASS",
            "candidate": str(runtime_home / "candidate" / source_sha),
            "source_sha": source_sha,
            "production": "NO_GO",
        },
    )

    with pytest.raises(AcceptedWorkCoverageError):
        runtime_deploy.stage(runtime_home, tmp_path, RESULT_SHA, 123, "MertSGI/AOS")
    assert loaded == []

    _ingest(
        KnowledgeLedger(runtime_home / "knowledge"),
        agent_class="HUMAN_OPERATOR",
        bootstrap_transition=True,
        idempotency_key="bootstrap-transition",
        evidence_refs=["operator-review:KCP-transition"],
    )
    result = runtime_deploy.stage(runtime_home, tmp_path, RESULT_SHA, 123, "MertSGI/AOS")

    assert result["stage"] == "PASS"
    assert loaded == [True]


def test_platform_source_recovery_receipt_failure_cannot_return_promotion_ready(
    tmp_path: Path, monkeypatch
):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=True)
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    original_append = ledger.append

    def fail_implementation(event_type, **kwargs):
        if KnowledgeEventType(event_type) == KnowledgeEventType.IMPLEMENTATION_RECEIPT:
            raise OSError("disk full")
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(ledger, "append", fail_implementation)
    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        BoundedSelfRepairEngine(tmp_path / "repair", diag),
        source_base_sha=BASE_SHA,
        knowledge_ledger=ledger,
        source_repair_executor=lambda _request: SourceRepairResult(
            isolated_worktree=str(tmp_path / "worktree"),
            branch="fix/repair",
            base_sha=BASE_SHA,
            repair_sha=RESULT_SHA,
            workspace_fingerprint="fingerprint",
            resource_backend_id="native_execution",
            attempt_telemetry={"attempt_count": 1},
            git_diff_sha256="e" * 64,
            candidate_manifest={"candidate_source_sha": RESULT_SHA, "build_source_sha": RESULT_SHA},
            rollback_information={"base_sha": BASE_SHA},
            tests_passed=True,
            evidence_valid=True,
            exact_sha_ci_status="SUCCESS",
            candidate_materialized=True,
        ),
    )

    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.FAILED_VERIFICATION.value
    assert job["blocker"] == "KCP_RECEIPT_WRITE_FAILED"


def test_platform_runtime_recovery_receipt_failure_cannot_return_repaired_verified(
    tmp_path: Path, monkeypatch
):
    diag = SelfDiagnosisEngine(tmp_path / "diagnosis")
    finding = _finding(diag, requires_candidate=False)
    repair = BoundedSelfRepairEngine(tmp_path / "repair", diag)
    repair.set_live_mode(True)
    repair.register_actuator(
        "PROVIDER_TRANSIENT_FAILURE",
        lambda _finding, _proposal: RepairActuationResult(
            operation_performed=True,
            postcondition_verified=True,
            operation="OWNED_PROCESS_RESTART",
            evidence={"readyz": "PASS"},
            rollback_reference="owned-process-stop",
        ),
    )
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    original_append = ledger.append

    def fail_implementation(event_type, **kwargs):
        if KnowledgeEventType(event_type) == KnowledgeEventType.IMPLEMENTATION_RECEIPT:
            raise OSError("disk full")
        return original_append(event_type, **kwargs)

    monkeypatch.setattr(ledger, "append", fail_implementation)
    coordinator = PlatformRecoveryCoordinator(
        tmp_path / "platform-recovery",
        repair,
        source_base_sha=BASE_SHA,
        knowledge_ledger=ledger,
    )

    job = coordinator.process_finding(finding.finding_id)

    assert job["disposition"] == RepairDisposition.FAILED_VERIFICATION.value
    assert job["blocker"] == "KCP_RECEIPT_WRITE_FAILED"


def test_resolution_and_ingress_never_mutate_process_environment(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AOS_KNOWLEDGE_HOME", raising=False)
    runtime_home = tmp_path / "runtime-v1"

    ledger = resolve_knowledge_ledger(runtime_home=runtime_home)
    assert ledger is not None
    _ingest(ledger)

    assert "AOS_KNOWLEDGE_HOME" not in __import__("os").environ


def test_failed_verification_receipt_and_non_receipt_events_never_count(tmp_path: Path):
    ledger = KnowledgeLedger(tmp_path / "knowledge")
    record_implementation_receipt(
        ledger,
        project_id="AOS",
        idempotency_key="impl-only",
        agent_class="CODEX",
        tool_name="pytest",
        base_sha=BASE_SHA,
        result_sha=RESULT_SHA,
    )
    record_verification_receipt(
        ledger,
        project_id="AOS",
        idempotency_key="failed-verification",
        agent_class="CODEX",
        tool_name="pytest",
        result_sha=RESULT_SHA,
        verification={"status": "FAILED"},
    )

    assert accepted_work_coverage(ledger, project_id="AOS", result_sha=RESULT_SHA)["covered"] is False


def test_external_cli_ingress_and_preflight_contract(tmp_path: Path, capsys):
    runtime_home = tmp_path / "runtime-v1"
    code = knowledge_main([
        "--runtime-home", str(runtime_home),
        "--project-id", "AOS",
        "ingest-work",
        "--agent-class", "CLINE",
        "--tool-name", "cline",
        "--base-sha", BASE_SHA,
        "--result-sha", RESULT_SHA,
        "--repository", "MertSGI/AOS",
        "--branch", "fix/cli-test",
        "--module-id", "KCP",
        "--changed-path", "src/aos/knowledge/ingress.py",
        "--evidence-ref", "ci-run:456",
        "--verification-status", "PASS",
        "--canonical-next-action", "Review candidate",
        "--idempotency-key", "cli-ingress",
    ])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["KCP_ARCHIVAL_STATUS"] == "KCP_ARCHIVED"
    assert knowledge_main([
        "--runtime-home", str(runtime_home),
        "--project-id", "AOS",
        "context",
        "--task-class", "SOURCE_CHANGE",
        "--module-id", "KCP",
        "--path", "src/aos/knowledge/ingress.py",
        "--base-sha", RESULT_SHA,
    ]) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["context_pack_hash"] == context["context_hash"]
    assert context["ledger_sequence"] == 2
    assert "current_decisions" in context
    assert "relevant_invariants" in context
    assert "module_relationships" in context
    assert "active_blockers" in context
    assert "unresolved_audit_findings" in context
    assert "latest_implementations" in context
    assert "latest_verifications" in context
    assert "canonical_next_action" in context
