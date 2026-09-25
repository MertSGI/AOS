"""AOS-Native Self-Diagnosis and Self-Maintenance (Shadow Only).

This module implements AOS-native self-observation, defect classification,
durable fingerprinting/deduplication, repair authority classification,
and structured shadow repair proposal generation.

INVARIANTS:
- Phase is strictly SHADOW_ONLY.
- Never edit AOS source, create repair commits, or promote candidates in this phase.
- Consume real machine/runtime evidence only (never diagnose from intuition alone).
- Restart safe: deduplication and recurrence state persists across process restarts.
- Severity and autonomy impact are deterministic and bound to observable consequences.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aos.process_utils import run_headless
from aos.provenance import is_valid_full_sha
from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, read_json

SELF_DIAGNOSIS_SCHEMA_VERSION = "1.0.0"

# Permitted finding status lifecycle
STATUS_OBSERVED = "OBSERVED"
STATUS_CLASSIFIED = "CLASSIFIED"
STATUS_SHADOW_REPAIR_PROPOSED = "SHADOW_REPAIR_PROPOSED"
STATUS_RESOLVED_WITHOUT_REPAIR = "RESOLVED_WITHOUT_REPAIR"
STATUS_SUPPRESSED_DUPLICATE = "SUPPRESSED_DUPLICATE"
STATUS_HUMAN_REQUIRED = "HUMAN_REQUIRED"

# Permitted defect classifications
DEFECT_CLASSES = {
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

# Permitted severities
SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}

# Permitted autonomy impacts
AUTONOMY_IMPACTS = {
    "NONE",
    "DEGRADED",
    "BLOCKING_SINGLE_LANE",
    "BLOCKING_MULTI_LANE",
    "BLOCKING_AOS_RUNTIME",
}

# Permitted repair authority classifications
REPAIR_AUTHORITIES = {
    "ROUTINE_SELF_REPAIR_ELIGIBLE",
    "ISOLATED_CANDIDATE_REQUIRED",
    "HUMAN_REQUIRED",
    "FORBIDDEN",
}


@dataclass
class ShadowRepairProposal:
    problem: str
    evidence: List[str]
    root_cause_hypothesis: str
    minimal_change: str
    files_likely_affected: List[str]
    tests_required: List[str]
    ci_required: bool
    runtime_proof_required: str
    rollback_plan: str
    authority_class: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SelfDiagnosisFinding:
    finding_id: str
    fingerprint: str
    detected_at: str
    component: str
    symptom: str
    failure_class: str
    severity: str
    recurrence_count: int
    first_seen_at: str
    last_seen_at: str
    autonomy_impact: str
    affected_lane_ids: List[str]
    evidence_refs: List[str]
    evidence_class: str
    confidence: float
    suspected_root_cause: str
    repair_class: str
    repair_authority: str
    proposed_repair: Optional[Dict[str, Any]]
    required_tests: List[str]
    required_runtime_proof: str
    requires_candidate: bool
    requires_human: bool
    status: str
    episode_id: str = ""
    last_observed_at: str = ""
    last_recurrence_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_evidence: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SelfDiagnosisEngine:
    """AOS-native deterministic shadow self-diagnosis engine."""

    def __init__(self, diagnosis_root: Path, runtime_config: Optional[Dict[str, Any]] = None) -> None:
        self.diagnosis_root = diagnosis_root.expanduser().resolve()
        self.diagnosis_root.mkdir(parents=True, exist_ok=True)
        self.findings_dir = self.diagnosis_root / "findings"
        self.findings_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.diagnosis_root / "dedup-index.json"
        self.runtime_config = runtime_config or {}
        self.cooldown_seconds = 300.0  # 5 min cooldown before creating separate finding if not deduped
        self._index: Dict[str, Dict[str, Any]] = self._load_index()

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        return read_json(self.state_file, {})

    def _save_index(self) -> None:
        atomic_json(self.state_file, self._index)

    def compute_fingerprint(self, component: str, failure_class: str, symptom: str) -> str:
        raw = f"{component.strip().lower()}:{failure_class.strip().upper()}:{symptom.strip().lower()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def record_or_update_finding(
        self,
        component: str,
        failure_class: str,
        symptom: str,
        severity: str,
        autonomy_impact: str,
        affected_lane_ids: List[str],
        evidence_refs: List[str],
        evidence_class: str,
        confidence: float,
        suspected_root_cause: str,
        repair_authority: str,
        repair_class: str = "BOUNDED_MAINTENANCE",
        proposed_repair: Optional[ShadowRepairProposal] = None,
        required_tests: Optional[List[str]] = None,
        required_runtime_proof: str = "PASSING_UNIT_AND_CANONICAL_TESTS",
        requires_candidate: bool = False,
        requires_human: bool = False,
        status: str = STATUS_CLASSIFIED,
    ) -> SelfDiagnosisFinding:
        """Create or update a deduplicated finding based on stable fingerprint.
        
        Recurrence semantics:
        - Repeated observations in the same unresolved episode update timestamps/evidence
          without incrementing recurrence_count.
        - If finding was previously RESOLVED_WITHOUT_REPAIR, observation starts a new episode
          and increments recurrence_count exactly once.
        """
        now_utc = utc_now()
        fp = self.compute_fingerprint(component, failure_class, symptom)

        if failure_class not in DEFECT_CLASSES:
            failure_class = "UNKNOWN"
        if severity not in SEVERITIES:
            severity = "MEDIUM"
        if autonomy_impact not in AUTONOMY_IMPACTS:
            autonomy_impact = "DEGRADED"
        if repair_authority not in REPAIR_AUTHORITIES:
            repair_authority = "HUMAN_REQUIRED"

        existing = self._index.get(fp)
        if existing:
            # Update existing finding
            finding_id = existing["finding_id"]
            finding_path = self.findings_dir / f"{finding_id}.json"
            finding_data = read_json(finding_path, {})
            current_status = finding_data.get("status", existing.get("status", status))
            recurrence = int(finding_data.get("recurrence_count", existing.get("recurrence_count", 1)))
            first_seen = finding_data.get("first_seen_at", existing.get("first_seen_at", now_utc))
            episode_id = finding_data.get("episode_id") or existing.get("episode_id") or f"ep-{int(time.time())}-{fp[:6]}"
            last_recurrence_at = finding_data.get("last_recurrence_at")

            # Check if defect is returning from resolved state
            is_reopened = False
            if current_status == STATUS_RESOLVED_WITHOUT_REPAIR:
                # Return of a resolved defect: new recurrence episode
                recurrence += 1
                episode_id = f"ep-{int(time.time())}-r{recurrence}-{fp[:6]}"
                last_recurrence_at = now_utc
                current_status = STATUS_SHADOW_REPAIR_PROPOSED if proposed_repair else (
                    STATUS_HUMAN_REQUIRED if requires_human else status
                )
                is_reopened = True
            else:
                # Same unresolved episode: do NOT increment recurrence_count
                if proposed_repair:
                    current_status = STATUS_SHADOW_REPAIR_PROPOSED
                elif requires_human:
                    current_status = STATUS_HUMAN_REQUIRED

            # Reset clean cycles since defect is currently observed
            existing["clean_cycles"] = 0

            # Update mutable fields
            finding_data["recurrence_count"] = recurrence
            finding_data["last_seen_at"] = now_utc
            finding_data["last_observed_at"] = now_utc
            finding_data["last_recurrence_at"] = last_recurrence_at
            finding_data["episode_id"] = episode_id
            finding_data["status"] = current_status
            finding_data["resolved_at"] = None if is_reopened else finding_data.get("resolved_at")
            finding_data["resolution_evidence"] = None if is_reopened else finding_data.get("resolution_evidence")
            finding_data["evidence_refs"] = list(set(finding_data.get("evidence_refs", []) + evidence_refs))
            finding_data["affected_lane_ids"] = list(set(finding_data.get("affected_lane_ids", []) + affected_lane_ids))
            if proposed_repair:
                finding_data["proposed_repair"] = proposed_repair.to_dict()

            atomic_json(finding_path, finding_data)
            self._index[fp]["recurrence_count"] = recurrence
            self._index[fp]["last_seen_at"] = now_utc
            self._index[fp]["last_observed_at"] = now_utc
            self._index[fp]["last_recurrence_at"] = last_recurrence_at
            self._index[fp]["episode_id"] = episode_id
            self._index[fp]["status"] = current_status
            self._index[fp]["clean_cycles"] = 0
            self._save_index()

            return SelfDiagnosisFinding(
                finding_id=finding_id,
                fingerprint=fp,
                detected_at=finding_data["detected_at"],
                component=component,
                symptom=symptom,
                failure_class=failure_class,
                severity=severity,
                recurrence_count=recurrence,
                first_seen_at=first_seen,
                last_seen_at=now_utc,
                autonomy_impact=autonomy_impact,
                affected_lane_ids=finding_data["affected_lane_ids"],
                evidence_refs=finding_data["evidence_refs"],
                evidence_class=evidence_class,
                confidence=confidence,
                suspected_root_cause=suspected_root_cause,
                repair_class=repair_class,
                repair_authority=repair_authority,
                proposed_repair=finding_data.get("proposed_repair"),
                required_tests=required_tests or [],
                required_runtime_proof=required_runtime_proof,
                requires_candidate=requires_candidate,
                requires_human=requires_human,
                status=current_status,
                episode_id=episode_id,
                last_observed_at=now_utc,
                last_recurrence_at=last_recurrence_at,
                resolved_at=finding_data.get("resolved_at"),
                resolution_evidence=finding_data.get("resolution_evidence"),
            )

        # New finding
        finding_id = f"diag-{int(time.time())}-{fp[:8]}"
        initial_status = STATUS_SHADOW_REPAIR_PROPOSED if proposed_repair else (
            STATUS_HUMAN_REQUIRED if requires_human else status
        )
        episode_id = f"ep-{int(time.time())}-{fp[:6]}"

        finding = SelfDiagnosisFinding(
            finding_id=finding_id,
            fingerprint=fp,
            detected_at=now_utc,
            component=component,
            symptom=symptom,
            failure_class=failure_class,
            severity=severity,
            recurrence_count=1,
            first_seen_at=now_utc,
            last_seen_at=now_utc,
            autonomy_impact=autonomy_impact,
            affected_lane_ids=affected_lane_ids,
            evidence_refs=evidence_refs,
            evidence_class=evidence_class,
            confidence=confidence,
            suspected_root_cause=suspected_root_cause,
            repair_class=repair_class,
            repair_authority=repair_authority,
            proposed_repair=proposed_repair.to_dict() if proposed_repair else None,
            required_tests=required_tests or [],
            required_runtime_proof=required_runtime_proof,
            requires_candidate=requires_candidate,
            requires_human=requires_human,
            status=initial_status,
            episode_id=episode_id,
            last_observed_at=now_utc,
            last_recurrence_at=now_utc,
            resolved_at=None,
            resolution_evidence=None,
        )

        finding_path = self.findings_dir / f"{finding_id}.json"
        atomic_json(finding_path, finding.to_dict())

        self._index[fp] = {
            "finding_id": finding_id,
            "failure_class": failure_class,
            "component": component,
            "severity": severity,
            "recurrence_count": 1,
            "first_seen_at": now_utc,
            "last_seen_at": now_utc,
            "last_observed_at": now_utc,
            "last_recurrence_at": now_utc,
            "episode_id": episode_id,
            "status": initial_status,
            "clean_cycles": 0,
        }
        self._save_index()
        return finding

    def reconcile_active_findings(
        self,
        observed_fingerprints: Set[str],
        clean_evidence: str = "2 consecutive clean diagnostic cycles without defect observation",
    ) -> List[Dict[str, Any]]:
        """Reconcile active findings against currently observed fingerprints.
        
        Previously active findings absent from current evidence enter a bounded confirmation
        process. After 2 consecutive clean cycles, status transitions to RESOLVED_WITHOUT_REPAIR.
        """
        now_utc = utc_now()
        resolved_findings: List[Dict[str, Any]] = []

        for fp, meta in self._index.items():
            status = meta.get("status", "")
            if status in (STATUS_RESOLVED_WITHOUT_REPAIR, STATUS_SUPPRESSED_DUPLICATE):
                continue

            if fp in observed_fingerprints:
                # Still observed: reset clean counter
                meta["clean_cycles"] = 0
            else:
                # Absent from evidence in this cycle: increment clean counter
                clean_count = int(meta.get("clean_cycles", 0)) + 1
                meta["clean_cycles"] = clean_count

                if clean_count >= 2:
                    # Mark resolved without repair
                    meta["status"] = STATUS_RESOLVED_WITHOUT_REPAIR
                    meta["resolved_at"] = now_utc

                    finding_id = meta["finding_id"]
                    finding_path = self.findings_dir / f"{finding_id}.json"
                    finding_data = read_json(finding_path, {})
                    if finding_data:
                        finding_data["status"] = STATUS_RESOLVED_WITHOUT_REPAIR
                        finding_data["resolved_at"] = now_utc
                        finding_data["resolution_evidence"] = clean_evidence
                        atomic_json(finding_path, finding_data)
                        resolved_findings.append(finding_data)

        self._save_index()
        return resolved_findings

    def get_finding(self, finding_id: str) -> Optional[Dict[str, Any]]:
        path = self.findings_dir / f"{finding_id}.json"
        if path.is_file():
            return read_json(path, {})
        return None

    def record_resolution(self, finding_id: str, evidence: str) -> bool:
        path = self.findings_dir / f"{finding_id}.json"
        if not path.is_file():
            return False
        data = read_json(path, {})
        if not data:
            return False
        now_utc = utc_now()
        data["status"] = STATUS_RESOLVED_WITHOUT_REPAIR
        data["resolved_at"] = now_utc
        data["resolution_evidence"] = evidence
        atomic_json(path, data)
        fp = data.get("fingerprint")
        if fp and fp in self._index:
            self._index[fp]["status"] = STATUS_RESOLVED_WITHOUT_REPAIR
            self._index[fp]["resolved_at"] = now_utc
            self._save_index()
        return True

    def list_findings(self) -> List[Dict[str, Any]]:
        findings = []
        for p in self.findings_dir.glob("diag-*.json"):
            data = read_json(p, {})
            if data:
                findings.append(data)
        # Order findings by last_seen_at / last_observed_at descending, fallback to detected_at
        findings.sort(
            key=lambda x: str(x.get("last_seen_at") or x.get("last_observed_at") or x.get("detected_at", "")),
            reverse=True,
        )
        return findings

    def diagnose_runtime(
        self,
        *,
        runtime_health: Dict[str, Any],
        lanes: List[Dict[str, Any]],
        relay_snapshot: Optional[Dict[str, Any]] = None,
        provenance_status: str = "UNPROVEN",
        build_source_sha: Optional[str] = None,
        candidate_manifest_sha: Optional[str] = None,
        runtime_source_sha: Optional[str] = None,
        local_git_head: Optional[str] = None,
        outbox_status: str = "DISABLED",
    ) -> List[SelfDiagnosisFinding]:
        """Execute shadow diagnostic analysis across machine/runtime evidence."""
        findings: List[SelfDiagnosisFinding] = []

        # 1. Inspect Provenance Evidence
        if provenance_status == "FAIL":
            prop = ShadowRepairProposal(
                problem="Literal equality failure across authoritative provenance chain",
                evidence=[
                    f"LOCAL_GIT_HEAD={local_git_head}",
                    f"BUILD_SOURCE_SHA={build_source_sha}",
                    f"CANDIDATE_MANIFEST_SHA={candidate_manifest_sha}",
                    f"RUNTIME_SOURCE_SHA={runtime_source_sha}",
                ],
                root_cause_hypothesis="Slot materialized with mismatched source SHA or local Git checkout differs from active slot",
                minimal_change="Materialize new candidate from exact Git HEAD and verify exact string equality",
                files_likely_affected=["src/aos/provenance.py", "materialize_slot.py"],
                tests_required=["tests/test_provenance.py"],
                ci_required=True,
                runtime_proof_required="EXACT_SHA_CI_PROVEN_AND_CANDIDATE_MATERIALIZED",
                rollback_plan="Roll back to previous proven slot or rebuild from authoritative Git HEAD",
                authority_class="ISOLATED_CANDIDATE_REQUIRED",
            )
            f = self.record_or_update_finding(
                component="provenance",
                failure_class="PROVENANCE_FAILURE",
                symptom=f"Provenance validation returned FAIL (git={local_git_head[:8] if local_git_head else 'NONE'} runtime={runtime_source_sha[:8] if runtime_source_sha else 'NONE'})",
                severity="CRITICAL",
                autonomy_impact="BLOCKING_AOS_RUNTIME",
                affected_lane_ids=[l.get("lane_id", "") for l in lanes],
                evidence_refs=["provenance_validator"],
                evidence_class="DURABLE_PROVENANCE_MISMATCH",
                confidence=1.0,
                suspected_root_cause="Candidate manifest or runtime SHA does not match authoritative Git checkout",
                repair_authority="ISOLATED_CANDIDATE_REQUIRED",
                proposed_repair=prop,
                requires_candidate=True,
                requires_human=False,
            )
            findings.append(f)

        # 2. Inspect Runtime Health & Process Failures
        r_state = runtime_health.get("runtime_state", "UNKNOWN")
        if r_state in ("UNHEALTHY", "FAILED", "CORRUPTED"):
            prop = ShadowRepairProposal(
                problem=f"Runtime server reporting state {r_state}",
                evidence=[f"runtime_state={r_state}", f"pid={runtime_health.get('pid')}"],
                root_cause_hypothesis="Runtime server experienced internal exception or unhandled fault",
                minimal_change="Inspect runtime supervisor logs and restart runtime child process",
                files_likely_affected=["src/aos/runtime_server.py"],
                tests_required=["tests/test_runtime_server.py"],
                ci_required=False,
                runtime_proof_required="HTTP_GET_V1_HEALTH_200_HEALTHY",
                rollback_plan="Supervisor rolls back candidate slot if restart limit exceeded",
                authority_class="ROUTINE_SELF_REPAIR_ELIGIBLE",
            )
            f = self.record_or_update_finding(
                component="runtime_server",
                failure_class="RUNTIME_PROCESS_FAILURE",
                symptom=f"Runtime API health is {r_state}",
                severity="HIGH",
                autonomy_impact="BLOCKING_AOS_RUNTIME",
                affected_lane_ids=[l.get("lane_id", "") for l in lanes],
                evidence_refs=["/v1/health"],
                evidence_class="MACHINE_HTTP_HEALTH",
                confidence=0.95,
                suspected_root_cause="Runtime server process failed health check",
                repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
                proposed_repair=prop,
                requires_candidate=False,
                requires_human=False,
            )
            findings.append(f)

        # 3. Inspect Lane States, Provider Failures & Human Required
        for lane in lanes:
            lane_name = lane.get("lane_id") or "Lane Unknown"
            l_state = str(lane.get("state", "UNKNOWN"))
            proj_id = lane.get("project_id", "")
            cmd_id = lane.get("command_id", "")
            attempts = int(lane.get("attempts", 0) or 0)
            batches = int(lane.get("completed_batches", 0) or 0)
            backoff = bool(lane.get("provider_backoff", False))

            if l_state == "HUMAN_REQUIRED":
                prop = ShadowRepairProposal(
                    problem=f"{lane_name} entered HUMAN_REQUIRED state",
                    evidence=[f"command_id={cmd_id}", f"project_id={proj_id}", f"attempts={attempts}"],
                    root_cause_hypothesis="Policy red-line, critical boundary or unresolvable ambiguity reached",
                    minimal_change="Operator review required to adjust goal or grant permission",
                    files_likely_affected=[],
                    tests_required=[],
                    ci_required=False,
                    runtime_proof_required="OPERATOR_INSPECTION_AND_RESUME",
                    rollback_plan="No automatic modification permitted",
                    authority_class="HUMAN_REQUIRED",
                )
                f = self.record_or_update_finding(
                    component=f"lane_{proj_id}",
                    failure_class="UNKNOWN",
                    symptom=f"{lane_name} ({proj_id}) transitioned to HUMAN_REQUIRED",
                    severity="HIGH",
                    autonomy_impact="BLOCKING_SINGLE_LANE",
                    affected_lane_ids=[lane_name],
                    evidence_refs=[f"commands/{cmd_id}/state.json"],
                    evidence_class="LANE_STATE_HUMAN_REQUIRED",
                    confidence=1.0,
                    suspected_root_cause="Execution stopped at standing authority boundary",
                    repair_authority="HUMAN_REQUIRED",
                    proposed_repair=prop,
                    requires_candidate=False,
                    requires_human=True,
                    status=STATUS_HUMAN_REQUIRED,
                )
                findings.append(f)

            if backoff or l_state == "WAITING_FOR_REASONING_PROVIDER":
                all_down = bool(runtime_health.get("all_reasoning_providers_unavailable", False))
                circuits_open = int(runtime_health.get("provider_circuits_open", 0) or 0)
                next_probe = runtime_health.get("next_provider_probe_at")
                last_success = runtime_health.get("last_provider_success")
                healthy_count = int(runtime_health.get("healthy_reasoning_provider_count", 0) or 0)

                failure_class = "PROVIDER_ALL_UNAVAILABLE" if all_down else ("PROVIDER_TRANSIENT_FAILURE" if attempts < 5 else "PROVIDER_PERSISTENT_FAILURE")
                severity = "HIGH" if all_down else ("LOW" if attempts < 3 else "MEDIUM")
                symptom = (
                    "All authorized reasoning providers unavailable; circuits open with adaptive backoff"
                    if all_down
                    else f"{lane_name} ({proj_id}) waiting for reasoning provider availability"
                )

                prop = ShadowRepairProposal(
                    problem=symptom,
                    evidence=[
                        f"command_id={cmd_id}",
                        f"backoff={backoff}",
                        f"state={l_state}",
                        f"circuits_open={circuits_open}",
                        f"healthy_count={healthy_count}",
                        f"next_probe_at={next_probe}",
                        f"last_success={last_success}",
                    ],
                    root_cause_hypothesis="Provider API quota exhausted, rate limited, or model endpoint offline",
                    minimal_change="Durable circuit breaker will probe on schedule; healthy fallback provider executes automatically",
                    files_likely_affected=["src/aos/provider_circuit.py", "src/aos/autonomous_host.py"],
                    tests_required=["tests/test_provider_circuit.py"],
                    ci_required=False,
                    runtime_proof_required="PROVIDER_PROBE_SUCCESS",
                    rollback_plan="Automatic adaptive backoff schedule",
                    authority_class="ROUTINE_SELF_REPAIR_ELIGIBLE",
                )
                f = self.record_or_update_finding(
                    component="reasoning_provider",
                    failure_class=failure_class,
                    symptom=symptom,
                    severity=severity,
                    autonomy_impact="BLOCKING_MULTI_LANE" if all_down else "DEGRADED",
                    affected_lane_ids=[lane_name],
                    evidence_refs=[f"commands/{cmd_id}/state.json", "provider-circuits.json"],
                    evidence_class="PROVIDER_CIRCUIT_TELEMETRY",
                    confidence=0.95,
                    suspected_root_cause="Reasoning provider circuit open with adaptive backoff schedule",
                    repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
                    proposed_repair=prop,
                    requires_candidate=False,
                    requires_human=False,
                )
                findings.append(f)

        # 4. Inspect Remote Outbox
        if outbox_status == "HUMAN_REQUIRED_AUTH_SETUP":
            f = self.record_or_update_finding(
                component="remote_outbox",
                failure_class="REMOTE_OUTBOX_FAILURE",
                symptom="GitHub remote outbox cannot publish: GitHub CLI/token unauthenticated",
                severity="INFO",
                autonomy_impact="NONE",
                affected_lane_ids=[],
                evidence_refs=["gh auth status"],
                evidence_class="LOCAL_CLI_AUTH_CHECK",
                confidence=1.0,
                suspected_root_cause="GitHub session credentials have not been configured on host",
                repair_authority="HUMAN_REQUIRED",
                requires_candidate=False,
                requires_human=True,
                status=STATUS_HUMAN_REQUIRED,
            )
            findings.append(f)

        # 5. Inspect Stagnation / No Forward Progress
        if relay_snapshot and relay_snapshot.get("forward_progress") == "NO":
            reason = relay_snapshot.get("no_progress_reason") or "ACTIVE_WITHOUT_MEANINGFUL_DELTA"
            if relay_snapshot.get("running_lane_count", 0) > 0:
                f = self.record_or_update_finding(
                    component="execution_progress",
                    failure_class="NO_FORWARD_PROGRESS",
                    symptom=f"Workers active but no completed batch delta: {reason}",
                    severity="LOW",
                    autonomy_impact="DEGRADED",
                    affected_lane_ids=[l.get("lane_id", "") for l in lanes if l.get("state") in ("RUNNING", "RECOVERING", "EXECUTING")],
                    evidence_refs=["controller_relay/LATEST.json"],
                    evidence_class="RELAY_TELEMETRY_DELTA",
                    confidence=0.85,
                    suspected_root_cause="Long-running batch execution or retry loop without successful advance",
                    repair_authority="ROUTINE_SELF_REPAIR_ELIGIBLE",
                    requires_candidate=False,
                    requires_human=False,
                )
                findings.append(f)

        return findings

    def summarize_status(self) -> Dict[str, Any]:
        """Produce a sanitized summary of active shadow diagnosis metrics."""
        findings = self.list_findings()
        active_count = len([f for f in findings if f.get("status") not in (STATUS_RESOLVED_WITHOUT_REPAIR, STATUS_SUPPRESSED_DUPLICATE)])
        blocking_count = len([
            f for f in findings
            if f.get("autonomy_impact") in ("BLOCKING_SINGLE_LANE", "BLOCKING_MULTI_LANE", "BLOCKING_AOS_RUNTIME")
            and f.get("status") not in (STATUS_RESOLVED_WITHOUT_REPAIR, STATUS_SUPPRESSED_DUPLICATE)
        ])
        recurring_count = len([f for f in findings if int(f.get("recurrence_count", 1)) > 1])
        new_count = len([f for f in findings if int(f.get("recurrence_count", 1)) == 1])

        last_f = findings[0] if findings else None

        if not findings or active_count == 0:
            diag_status = "HEALTHY_NO_ACTION"
        elif blocking_count > 0:
            diag_status = "DEFECTS_DETECTED_BLOCKING"
        else:
            diag_status = "DEFECTS_DETECTED_DEGRADED"

        return {
            "self_diagnosis_mode": "SHADOW_ONLY",
            "self_diagnosis_status": diag_status,
            "active_finding_count": active_count,
            "blocking_finding_count": blocking_count,
            "recurring_finding_count": recurring_count,
            "new_finding_count": new_count,
            "last_diagnosis_at": last_f.get("last_seen_at") if last_f else None,
            "last_finding_id": last_f.get("finding_id") if last_f else "NONE",
            "last_failure_class": last_f.get("failure_class") if last_f else "NONE",
            "last_finding_severity": last_f.get("severity") if last_f else "NONE",
            "last_autonomy_impact": last_f.get("autonomy_impact") if last_f else "NONE",
            "self_repair_eligibility": "ELIGIBLE_PENDING_GATE" if active_count > 0 else "NO_ACTION_REQUIRED",
            "shadow_repair_proposal_status": "AVAILABLE" if (last_f and last_f.get("proposed_repair")) else "NONE",
            "findings": findings[:10],
        }
