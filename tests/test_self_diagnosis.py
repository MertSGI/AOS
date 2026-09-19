"""Comprehensive unit tests for Self-Diagnosis Engine, Shadow Repair, and Relay Truth Invariants."""
import json
import os
import tempfile
from pathlib import Path

import pytest

from aos.controller_relay import ControllerRelayPublisher, RelaySnapshot
from aos.provenance import validate_exact_sha_provenance
from aos.self_diagnosis import (
    DEFECT_CLASSES,
    SEVERITIES,
    AUTONOMY_IMPACTS,
    REPAIR_AUTHORITIES,
    STATUS_CLASSIFIED,
    STATUS_HUMAN_REQUIRED,
    STATUS_SHADOW_REPAIR_PROPOSED,
    SelfDiagnosisEngine,
    SelfDiagnosisFinding,
    ShadowRepairProposal,
)


def test_22_defect_classes_defined():
    """All 22 defect classes must be strictly recognized."""
    assert len(DEFECT_CLASSES) == 22
    expected = {
        "RUNTIME_PROCESS_FAILURE",
        "SUPERVISOR_FAILURE",
        "PROVIDER_TRANSIENT_FAILURE",
        "PROVIDER_PERSISTENT_FAILURE",
        "PLANNER_STAGNATION",
        "REPEATED_ACTION_LOOP",
        "REPEATED_FILE_READ_LOOP",
        "NO_FORWARD_PROGRESS",
        "PROVENANCE_FAILURE",
        "CANDIDATE_PROMOTION_FAILURE",
        "CHECKPOINT_RECOVERY_FAILURE",
        "BROWSER_CAPABILITY_FAILURE",
        "TEST_FAILURE",
        "CI_FAILURE",
        "RELAY_FAILURE",
        "REMOTE_OUTBOX_FAILURE",
        "COUNCIL_INTERFERENCE",
        "COUNCIL_QUALITY_DEGRADATION",
        "WRITE_SCOPE_COLLISION",
        "DUPLICATE_WORK",
        "LOST_ACCEPTED_WORK",
        "UNKNOWN",
    }
    assert DEFECT_CLASSES == expected


def test_severity_and_autonomy_impact_sets():
    """Severities and autonomy impacts must match bounded values."""
    assert SEVERITIES == {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert AUTONOMY_IMPACTS == {
        "NONE",
        "DEGRADED",
        "BLOCKING_SINGLE_LANE",
        "BLOCKING_MULTI_LANE",
        "BLOCKING_AOS_RUNTIME",
    }
    assert REPAIR_AUTHORITIES == {
        "ROUTINE_SELF_REPAIR_ELIGIBLE",
        "ISOLATED_CANDIDATE_REQUIRED",
        "HUMAN_REQUIRED",
        "FORBIDDEN",
    }


def test_finding_deduplication_and_persistence(tmp_path: Path):
    """Findings must be stably deduplicated by fingerprint and persist across restarts."""
    diag_root = tmp_path / "self-diagnosis"
    engine1 = SelfDiagnosisEngine(diag_root)

    # 1. Record initial finding
    f1 = engine1.record_or_update_finding(
        component="test_component",
        failure_class="TEST_FAILURE",
        symptom="Assertion failure in test_foo",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=["Lane A"],
        evidence_refs=["tests/test_foo.py"],
        evidence_class="PYTEST_TRACEBACK",
        confidence=0.99,
        suspected_root_cause="Mock returned unexpected code",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    assert f1.recurrence_count == 1
    initial_id = f1.finding_id

    # 2. Record same symptom again (same component + failure_class + symptom inside unresolved episode)
    f2 = engine1.record_or_update_finding(
        component="test_component",
        failure_class="TEST_FAILURE",
        symptom="Assertion failure in test_foo",
        severity="MEDIUM",
        autonomy_impact="DEGRADED",
        affected_lane_ids=["Lane A"],
        evidence_refs=["tests/test_foo.py:42"],
        evidence_class="PYTEST_TRACEBACK",
        confidence=0.99,
        suspected_root_cause="Mock returned unexpected code",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    assert f2.finding_id == initial_id
    # Inside the same unresolved episode, recurrence_count does NOT inflate
    assert f2.recurrence_count == 1

    # 3. Simulate process restart: instantiate new engine pointing to same directory
    engine2 = SelfDiagnosisEngine(diag_root)
    summary = engine2.summarize_status()
    assert summary["active_finding_count"] == 1
    assert summary["new_finding_count"] == 1
    assert summary["recurring_finding_count"] == 0
    assert summary["last_finding_id"] == initial_id
    assert summary["last_failure_class"] == "TEST_FAILURE"

    # Verify get_finding
    finding_dict = engine2.get_finding(initial_id)
    assert finding_dict is not None
    assert finding_dict["recurrence_count"] == 1
    assert finding_dict["finding_id"] == initial_id


def test_shadow_repair_proposal_generation(tmp_path: Path):
    """Shadow repair proposals must contain required non-executable repair guidance."""
    diag_root = tmp_path / "self-diagnosis"
    engine = SelfDiagnosisEngine(diag_root)

    proposal = ShadowRepairProposal(
        problem="Runtime health degraded due to unhandled provider error",
        evidence=["HTTP 500 on /v1/health"],
        root_cause_hypothesis="Provider connection timeout unhandled",
        minimal_change="Add exponential backoff with jitter",
        files_likely_affected=["src/aos/providers.py"],
        tests_required=["tests/test_providers.py"],
        ci_required=True,
        runtime_proof_required="HTTP_GET_V1_HEALTH_200",
        rollback_plan="Revert candidate slot to previous proven slot",
        authority_class="ISOLATED_CANDIDATE_REQUIRED",
    )

    f = engine.record_or_update_finding(
        component="runtime_server",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="HTTP 500 on /v1/health",
        severity="HIGH",
        autonomy_impact="BLOCKING_AOS_RUNTIME",
        affected_lane_ids=["Lane A", "Lane C"],
        evidence_refs=["/v1/health"],
        evidence_class="HTTP_STATUS_500",
        confidence=0.95,
        suspected_root_cause="Provider connection timeout unhandled",
        repair_authority="ISOLATED_CANDIDATE_REQUIRED",
        proposed_repair=proposal,
        requires_candidate=True,
    )
    assert f.status == STATUS_SHADOW_REPAIR_PROPOSED
    assert f.proposed_repair is not None
    assert f.proposed_repair["minimal_change"] == "Add exponential backoff with jitter"
    assert f.requires_candidate is True

    summary = engine.summarize_status()
    assert summary["self_diagnosis_status"] == "DEFECTS_DETECTED_BLOCKING"
    assert summary["blocking_finding_count"] == 1
    assert summary["shadow_repair_proposal_status"] == "AVAILABLE"


def test_0a_summary_prose_no_unevidenced_zeroes(tmp_path: Path):
    """Section 0.A: LATEST.md summary prose must never claim unevidenced zeroes."""
    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    snap = pub.collect_snapshot()
    assert snap.lost_accepted_work == "UNKNOWN"
    assert snap.duplicate_completed_work == "UNKNOWN"

    md = pub.render_markdown(snap)
    # The prose must NOT contain "Zero lost accepted work; zero duplicate completed work"
    assert "Zero lost accepted work" not in md
    assert "zero duplicate completed work" not in md
    assert "Lost accepted work: UNKNOWN" in md
    assert "duplicate completed work: UNKNOWN" in md


def test_0b_build_source_sha_cannot_alias_manifest_sha(tmp_path: Path):
    """Section 0.B: Missing build_source_sha must yield UNPROVEN, not PROVEN via aliasing."""
    relay_dir = tmp_path / "controller-relay"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir()

    sha = "0fbb6abcb6bb2166581ee21cd11da73a9eb6a66e"
    # Create candidate slot with manifest that lacks build_source_sha
    slot_root = tmp_path / "slot"
    slot_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "candidate_source_sha": sha,
        # intentionally no build_source_sha
    }
    (slot_root / "candidate-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    config = {
        "runtime_root": str(tmp_path / "state"),
        "runtime_slot_root": str(slot_root),
        "authoritative_repo_path": str(repo_dir),
    }

    pub = ControllerRelayPublisher(relay_dir, config)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("aos.controller_relay.get_authoritative_git_head", lambda _: sha)
        snap = pub.collect_snapshot(runtime_health_dict={
            "runtime_slot_root": str(slot_root),
            "runtime_source_sha": sha,
            "runtime_state": "HEALTHY",
        })
        # Since build_source_sha is missing and not aliased, provenance must be UNPROVEN
        assert snap.provenance_status == "UNPROVEN"


def test_0c_user_facing_ui_mutation_requires_git_diff(tmp_path: Path):
    """Section 0.C: Preexisting untouched UI files in workspace do NOT count as mutations."""
    relay_dir = tmp_path / "controller-relay"
    state_dir = tmp_path / "state" / "commands" / "continue-lane-c"
    state_dir.mkdir(parents=True, exist_ok=True)
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    # Preexisting App.tsx in workspace
    (ws_dir / "App.tsx").write_text("export default function App() { return <div/>; }", encoding="utf-8")

    (state_dir / "command.json").write_text(json.dumps({
        "project": {"project_id": "lari-ui-v2", "workspace": str(ws_dir)}
    }), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps({
        "state": "RUNNING",
        "completed_batch_count": 1,
    }), encoding="utf-8")

    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    # Mock git status porcelain to report no modified UI files (clean)
    with pytest.MonkeyPatch.context() as mp:
        class DummyProc:
            returncode = 0
            stdout = "?? .aos_workspace_active.lock\n"
        mp.setattr("aos.controller_relay.run_headless", lambda cmd, check=False: DummyProc())
        snap = pub.collect_snapshot()

        # Touchless App.tsx must NOT be reported as first_user_facing_ui_mutation
        assert snap.first_user_facing_ui_mutation is None


def test_diagnosis_does_not_run_on_every_heartbeat_poll(tmp_path: Path):
    """Section 1: Self-diagnosis must NOT execute on every supervisor/relay poll."""
    relay_dir = tmp_path / "controller-relay"
    config = {"runtime_root": str(tmp_path / "state")}
    pub = ControllerRelayPublisher(relay_dir, config)

    # Mock diagnose_runtime to count calls
    call_count = 0
    orig_diagnose = pub.diagnostics.diagnose_runtime

    def mock_diagnose(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return orig_diagnose(*args, **kwargs)

    pub.diagnostics.diagnose_runtime = mock_diagnose

    # First poll: initial diagnosis executes
    pub.collect_snapshot()
    assert call_count == 1

    # Second and third heartbeat polls with identical telemetry: diagnosis must NOT re-execute
    pub.collect_snapshot()
    pub.collect_snapshot()
    assert call_count == 1

    # Now simulate a meaningful event: lane state transition
    state_dir = tmp_path / "state" / "commands" / "continue-lane-a"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "command.json").write_text(json.dumps({
        "project": {"project_id": "lari", "workspace": str(tmp_path / "ws")}
    }), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps({
        "state": "WAITING_FOR_REASONING_PROVIDER",
        "completed_batch_count": 0,
    }), encoding="utf-8")

    # State transition triggers diagnosis
    pub.collect_snapshot()
    assert call_count == 2


def test_recurrence_episode_and_reconciliation(tmp_path: Path):
    """Sections 2 & 3: Recurrence count represents new episode; reconciliation confirmation resolves defect."""
    diag_root = tmp_path / "self-diagnosis"
    engine = SelfDiagnosisEngine(diag_root)

    # 1. Observation 1: new finding created with recurrence_count=1
    f1 = engine.record_or_update_finding(
        component="test_worker",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="Worker segfault on init",
        severity="HIGH",
        autonomy_impact="BLOCKING_SINGLE_LANE",
        affected_lane_ids=["Lane A"],
        evidence_refs=["stderr.log"],
        evidence_class="PROCESS_EXIT_SIGNAL",
        confidence=1.0,
        suspected_root_cause="Bad binary",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    assert f1.recurrence_count == 1
    assert f1.status == STATUS_CLASSIFIED
    ep1 = f1.episode_id
    assert ep1 != ""

    # 2. Repeated observations in same unresolved episode do NOT inflate recurrence_count
    f2 = engine.record_or_update_finding(
        component="test_worker",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="Worker segfault on init",
        severity="HIGH",
        autonomy_impact="BLOCKING_SINGLE_LANE",
        affected_lane_ids=["Lane A"],
        evidence_refs=["stderr.log"],
        evidence_class="PROCESS_EXIT_SIGNAL",
        confidence=1.0,
        suspected_root_cause="Bad binary",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    assert f2.recurrence_count == 1
    assert f2.episode_id == ep1

    # 3. Finding absent in 1 cycle: clean_cycles becomes 1, remains active
    resolved = engine.reconcile_active_findings(observed_fingerprints=set())
    assert len(resolved) == 0
    f_after_1 = engine.get_finding(f1.finding_id)
    assert f_after_1["status"] == STATUS_CLASSIFIED

    # 4. Finding absent in 2nd consecutive cycle: status transitions to RESOLVED_WITHOUT_REPAIR
    resolved = engine.reconcile_active_findings(observed_fingerprints=set())
    assert len(resolved) == 1
    assert resolved[0]["finding_id"] == f1.finding_id
    assert resolved[0]["status"] == "RESOLVED_WITHOUT_REPAIR"
    assert resolved[0]["resolved_at"] is not None
    assert "2 consecutive clean diagnostic cycles" in resolved[0]["resolution_evidence"]

    # Historical resolved finding does NOT count as active or blocking
    summary = engine.summarize_status()
    assert summary["active_finding_count"] == 0
    assert summary["blocking_finding_count"] == 0
    assert summary["self_diagnosis_status"] == "HEALTHY_NO_ACTION"

    # 5. Defect returns later: reopen existing fingerprint as a new episode and increment recurrence_count exactly once
    f3 = engine.record_or_update_finding(
        component="test_worker",
        failure_class="RUNTIME_PROCESS_FAILURE",
        symptom="Worker segfault on init",
        severity="HIGH",
        autonomy_impact="BLOCKING_SINGLE_LANE",
        affected_lane_ids=["Lane A"],
        evidence_refs=["stderr.log"],
        evidence_class="PROCESS_EXIT_SIGNAL",
        confidence=1.0,
        suspected_root_cause="Bad binary",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    assert f3.finding_id == f1.finding_id
    assert f3.recurrence_count == 2
    assert f3.episode_id != ep1
    assert f3.status == STATUS_CLASSIFIED
    assert f3.last_recurrence_at is not None

    summary2 = engine.summarize_status()
    assert summary2["active_finding_count"] == 1
    assert summary2["blocking_finding_count"] == 1
    assert summary2["recurring_finding_count"] == 1
    assert summary2["new_finding_count"] == 0


def test_remote_outbox_auth_finding_never_blocks_runtime(tmp_path: Path):
    """Section 4: Remote outbox HUMAN_REQUIRED_AUTH_SETUP with INFO/NONE must never make diagnosis status BLOCKING."""
    diag_root = tmp_path / "self-diagnosis"
    engine = SelfDiagnosisEngine(diag_root)

    # Run diagnosis with outbox_status="HUMAN_REQUIRED_AUTH_SETUP" and otherwise healthy runtime
    findings = engine.diagnose_runtime(
        runtime_health={"runtime_state": "HEALTHY"},
        lanes=[],
        outbox_status="HUMAN_REQUIRED_AUTH_SETUP",
        provenance_status="PROVEN",
    )
    assert len(findings) == 1
    outbox_f = findings[0]
    assert outbox_f.severity == "INFO"
    assert outbox_f.autonomy_impact == "NONE"

    summary = engine.summarize_status()
    assert summary["active_finding_count"] == 1
    assert summary["blocking_finding_count"] == 0
    # Must NOT be DEFECTS_DETECTED_BLOCKING!
    assert summary["self_diagnosis_status"] != "DEFECTS_DETECTED_BLOCKING"
    assert summary["self_diagnosis_status"] == "DEFECTS_DETECTED_DEGRADED"


def test_last_finding_selected_by_last_seen_at(tmp_path: Path):
    """Section 5: Last finding must be selected by last_seen_at descending, not immutable detected_at."""
    diag_root = tmp_path / "self-diagnosis"
    engine = SelfDiagnosisEngine(diag_root)

    # Finding A created first at 10:00
    fa = engine.record_or_update_finding(
        component="comp_a",
        failure_class="TEST_FAILURE",
        symptom="failure A",
        severity="LOW",
        autonomy_impact="DEGRADED",
        affected_lane_ids=[],
        evidence_refs=[],
        evidence_class="TEST",
        confidence=1.0,
        suspected_root_cause="cause A",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    # Mock detected_at to be earlier
    engine._index[fa.fingerprint]["last_seen_at"] = "2026-09-19T10:00:00Z"
    path_a = diag_root / "findings" / f"{fa.finding_id}.json"
    data_a = json.loads(path_a.read_text("utf-8"))
    data_a["detected_at"] = "2026-09-19T10:00:00Z"
    data_a["last_seen_at"] = "2026-09-19T10:00:00Z"
    path_a.write_text(json.dumps(data_a), encoding="utf-8")

    # Finding B created second at 11:00
    fb = engine.record_or_update_finding(
        component="comp_b",
        failure_class="TEST_FAILURE",
        symptom="failure B",
        severity="LOW",
        autonomy_impact="DEGRADED",
        affected_lane_ids=[],
        evidence_refs=[],
        evidence_class="TEST",
        confidence=1.0,
        suspected_root_cause="cause B",
        repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
    )
    path_b = diag_root / "findings" / f"{fb.finding_id}.json"
    data_b = json.loads(path_b.read_text("utf-8"))
    data_b["detected_at"] = "2026-09-19T11:00:00Z"
    data_b["last_seen_at"] = "2026-09-19T11:00:00Z"
    path_b.write_text(json.dumps(data_b), encoding="utf-8")

    # Now update Finding A again at 12:00 (its detected_at stays 10:00, but last_seen_at becomes 12:00)
    data_a["last_seen_at"] = "2026-09-19T12:00:00Z"
    path_a.write_text(json.dumps(data_a), encoding="utf-8")
    engine._index[fa.fingerprint]["last_seen_at"] = "2026-09-19T12:00:00Z"
    engine._save_index()

    # summarize_status should select Finding A as last finding because last_seen_at is 12:00
    summary = engine.summarize_status()
    assert summary["last_finding_id"] == fa.finding_id

