"""Platform recovery plane, separate from product execution commands.

The coordinator consumes durable self-diagnosis findings and produces bounded
SYSTEM_REPAIR_JOB records.  It cannot activate a runtime or promote a source
candidate; successful source work stops at PROMOTION_READY.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, exclusive_file_lock, read_json
from aos.knowledge.audit import record_audit_finding
from aos.knowledge.hooks import execution_context_preflight
from aos.knowledge.ledger import KnowledgeLedger
from aos.knowledge.receipts import record_implementation_receipt, record_verification_receipt
from aos.self_repair import (
    AUTHORITY_AUTO_REPAIR_ELIGIBLE,
    AUTHORITY_FORBIDDEN,
    AUTHORITY_HUMAN_APPROVAL_REQUIRED,
    BoundedSelfRepairEngine,
    classify_defect_repair_authority,
)


class RepairDisposition(str, Enum):
    REPAIRED_VERIFIED = "REPAIRED_VERIFIED"
    NO_REPAIR_REQUIRED = "NO_REPAIR_REQUIRED"
    PROMOTION_READY = "PROMOTION_READY"
    WAITING_FOR_RESOURCE = "WAITING_FOR_RESOURCE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    UNSAFE_TO_REPAIR = "UNSAFE_TO_REPAIR"
    FAILED_VERIFICATION = "FAILED_VERIFICATION"


@dataclass(frozen=True)
class SourceRepairResult:
    isolated_worktree: str
    branch: str
    base_sha: str
    repair_sha: str
    workspace_fingerprint: str
    resource_backend_id: str
    attempt_telemetry: Dict[str, Any]
    git_diff_sha256: str
    candidate_manifest: Dict[str, Any]
    rollback_information: Dict[str, Any]
    tests_passed: bool
    evidence_valid: bool
    exact_sha_ci_status: str
    candidate_materialized: bool
    promotion_performed: bool = False
    activation_performed: bool = False
    evidence: Optional[Dict[str, Any]] = None


SourceRepairExecutor = Callable[[Dict[str, Any]], SourceRepairResult]


class SourceRepairResourceUnavailable(RuntimeError):
    """The governed source-repair path has no currently eligible resource."""


class PlatformRecoveryCoordinator:
    """Durable recovery director with finite, auditable dispositions."""

    def __init__(
        self,
        recovery_root: Path,
        repair_engine: BoundedSelfRepairEngine,
        *,
        source_base_sha: str,
        source_repair_executor: Optional[SourceRepairExecutor] = None,
        knowledge_ledger: Optional[KnowledgeLedger] = None,
    ) -> None:
        self.root = recovery_root.expanduser().resolve()
        self.jobs_root = self.root / "jobs"
        self.index_path = self.root / "finding-job-index.json"
        self.lock_path = self.root / "platform-recovery.lock"
        self.repair_engine = repair_engine
        self.source_base_sha = source_base_sha
        self.source_repair_executor = source_repair_executor
        self.knowledge_ledger = knowledge_ledger

    @staticmethod
    def _job_id(finding: Dict[str, Any]) -> str:
        body = json.dumps({
            "finding_id": finding.get("finding_id"),
            "fingerprint": finding.get("fingerprint"),
            "episode_id": finding.get("episode_id"),
        }, sort_keys=True, separators=(",", ":"))
        return f"repair-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:24]}"

    def _persist(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job["job_id"])
        with exclusive_file_lock(self.lock_path):
            index = read_json(self.index_path, {})
            atomic_json(self.jobs_root / f"{job_id}.json", job)
            index[str(job["finding_id"])] = job_id
            atomic_json(self.index_path, index)
        return job

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return read_json(self.jobs_root / f"{job_id}.json", {})

    def list_recent_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return bounded durable recovery jobs, newest first."""
        if not self.jobs_root.is_dir():
            return []
        jobs = [
            read_json(path, {})
            for path in self.jobs_root.glob("repair-*.json")
        ]
        populated = [job for job in jobs if job]
        populated.sort(
            key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""),
            reverse=True,
        )
        return populated[: max(0, int(limit))]

    def _record_knowledge_finding(self, finding: Dict[str, Any], proposed: Dict[str, Any]) -> None:
        if self.knowledge_ledger is None:
            return
        finding_id = str(finding.get("finding_id") or "")
        record_audit_finding(
            self.knowledge_ledger,
            project_id="AOS",
            finding_id=finding_id,
            layer="PLATFORM_RECOVERY",
            severity=str(finding.get("severity") or "UNKNOWN"),
            claim=str(finding.get("symptom") or finding.get("failure_class") or finding_id),
            evidence=[str(finding.get("fingerprint") or "")],
            affected_modules=[str(finding.get("component") or "PlatformRecovery")],
            affected_paths=list(proposed.get("files_likely_affected") or ()),
            source_sha=self.source_base_sha,
        )

    def observe_finding(self, finding_id: str) -> Dict[str, Any]:
        """Create a durable classified job without executing an actuator."""
        finding = self.repair_engine.diag_engine.get_finding(finding_id)
        if not finding:
            raise ValueError(f"Finding not found: {finding_id}")
        job_id = self._job_id(finding)
        existing = self.get_job(job_id)
        if existing:
            return existing
        proposed = finding.get("proposed_repair") or {}
        authority = classify_defect_repair_authority(
            component=str(finding.get("component") or ""),
            failure_class=str(finding.get("failure_class") or ""),
            symptom=str(finding.get("symptom") or ""),
            files_affected=proposed.get("files_likely_affected") or (),
        )
        disposition = RepairDisposition.WAITING_FOR_RESOURCE.value
        blocker = "BOUNDED_ACTUATOR_DISPATCH_REQUIRED"
        if authority == AUTHORITY_FORBIDDEN:
            disposition = RepairDisposition.UNSAFE_TO_REPAIR.value
            blocker = "AUTHORITY_FORBIDDEN"
        elif authority == AUTHORITY_HUMAN_APPROVAL_REQUIRED:
            disposition = RepairDisposition.HUMAN_REQUIRED.value
            blocker = "HUMAN_APPROVAL_REQUIRED"
        elif finding.get("requires_candidate") or proposed.get("ci_required"):
            blocker = "ISOLATED_SOURCE_REPAIR_EXECUTOR_REQUIRED"
        self._record_knowledge_finding(finding, proposed)
        return self._persist({
            "contract_version": CONTRACT_VERSION,
            "job_type": "SYSTEM_REPAIR_JOB",
            "job_id": job_id,
            "finding_id": finding_id,
            "finding_fingerprint": finding.get("fingerprint"),
            "authority_class": authority,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "product_command_id": None,
            "activation_permitted": False,
            "promotion_permitted": False,
            "production": "NO_GO",
            "disposition": disposition,
            "blocker": blocker,
            "execution_started": False,
        })

    def process_finding(self, finding_id: str) -> Dict[str, Any]:
        finding = self.repair_engine.diag_engine.get_finding(finding_id)
        if not finding:
            raise ValueError(f"Finding not found: {finding_id}")
        job_id = self._job_id(finding)
        existing = self.get_job(job_id)
        if existing and existing.get("disposition") != RepairDisposition.WAITING_FOR_RESOURCE.value:
            return existing

        proposed = finding.get("proposed_repair") or {}
        self._record_knowledge_finding(finding, proposed)
        authority = classify_defect_repair_authority(
            component=str(finding.get("component") or ""),
            failure_class=str(finding.get("failure_class") or ""),
            symptom=str(finding.get("symptom") or ""),
            files_affected=proposed.get("files_likely_affected") or (),
        )
        job: Dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "job_type": "SYSTEM_REPAIR_JOB",
            "job_id": job_id,
            "finding_id": finding_id,
            "finding_fingerprint": finding.get("fingerprint"),
            "authority_class": authority,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "product_command_id": None,
            "activation_permitted": False,
            "promotion_permitted": False,
            "production": "NO_GO",
        }

        if str(finding.get("status")) in {"RESOLVED_WITHOUT_REPAIR", "REPAIRED_VERIFIED"}:
            job.update(disposition=RepairDisposition.NO_REPAIR_REQUIRED.value)
            return self._persist(job)
        if authority == AUTHORITY_FORBIDDEN:
            job.update(disposition=RepairDisposition.UNSAFE_TO_REPAIR.value)
            return self._persist(job)
        if authority == AUTHORITY_HUMAN_APPROVAL_REQUIRED:
            job.update(disposition=RepairDisposition.HUMAN_REQUIRED.value)
            return self._persist(job)

        requires_candidate = bool(finding.get("requires_candidate") or proposed.get("ci_required"))
        if requires_candidate:
            if self.source_repair_executor is None:
                job.update(
                    disposition=RepairDisposition.WAITING_FOR_RESOURCE.value,
                    blocker="SOURCE_REPAIR_EXECUTOR_UNAVAILABLE",
                )
                return self._persist(job)
            if self.knowledge_ledger is not None:
                execution_context_preflight(
                    self.knowledge_ledger,
                    project_id="AOS",
                    task_class="PLATFORM_RECOVERY",
                    module_ids=[str(finding.get("component") or "PlatformRecovery")],
                    paths=list(proposed.get("files_likely_affected") or ()),
                    base_sha=self.source_base_sha,
                )
            try:
                result = self.source_repair_executor({
                    "job_id": job_id,
                    "finding": finding,
                    "required_base_sha": self.source_base_sha,
                    "requirements": {
                        "isolated_worktree": True,
                        "exact_sha_ci": True,
                        "immutable_candidate": True,
                        "promotion": False,
                        "activation": False,
                    },
                })
            except SourceRepairResourceUnavailable as exc:
                job.update(
                    disposition=RepairDisposition.WAITING_FOR_RESOURCE.value,
                    blocker="SOURCE_REPAIR_RESOURCE_UNAVAILABLE",
                    error_class=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
                return self._persist(job)
            except Exception as exc:
                job.update(
                    disposition=RepairDisposition.FAILED_VERIFICATION.value,
                    error_class=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
                return self._persist(job)
            evidence = asdict(result)
            safe = (
                result.base_sha == self.source_base_sha
                and len(result.repair_sha) == 40
                and all(character in "0123456789abcdef" for character in result.repair_sha)
                and result.repair_sha != result.base_sha
                and bool(result.isolated_worktree)
                and bool(result.workspace_fingerprint)
                and bool(result.resource_backend_id)
                and bool(result.attempt_telemetry)
                and len(result.git_diff_sha256) == 64
                and (
                    result.candidate_manifest.get("candidate_source_sha")
                    or result.candidate_manifest.get("source_sha")
                ) == result.repair_sha
                and (
                    "build_source_sha" not in result.candidate_manifest
                    or result.candidate_manifest.get("build_source_sha")
                    == result.repair_sha
                )
                and bool(result.rollback_information)
                and result.tests_passed
                and result.evidence_valid
                and result.exact_sha_ci_status == "SUCCESS"
                and result.candidate_materialized
                and not result.promotion_performed
                and not result.activation_performed
            )
            job.update(
                disposition=(
                    RepairDisposition.PROMOTION_READY.value
                    if safe
                    else RepairDisposition.FAILED_VERIFICATION.value
                ),
                source_repair=evidence,
            )
            if safe and self.knowledge_ledger is not None:
                common = {
                    "project_id": "AOS",
                    "agent_class": "AOS_NATIVE",
                    "tool_name": "aos.platform_recovery",
                    "base_sha": result.base_sha,
                    "result_sha": result.repair_sha,
                    "module_ids": [str(finding.get("component") or "PlatformRecovery")],
                    "changed_paths": list(proposed.get("files_likely_affected") or ()),
                    "evidence_refs": [result.git_diff_sha256],
                }
                record_implementation_receipt(
                    self.knowledge_ledger,
                    idempotency_key=f"recovery-implementation:{job_id}:{result.repair_sha}",
                    claims={"finding_id": finding_id, "promotion_performed": False},
                    **common,
                )
                record_verification_receipt(
                    self.knowledge_ledger,
                    idempotency_key=f"recovery-verification:{job_id}:{result.repair_sha}",
                    verification={"status": result.exact_sha_ci_status, "tests_passed": result.tests_passed},
                    claims={"finding_id": finding_id, "candidate_materialized": True},
                    **common,
                )
            return self._persist(job)

        if authority == AUTHORITY_AUTO_REPAIR_ELIGIBLE:
            if not self.repair_engine.live_active:
                job.update(
                    disposition=RepairDisposition.WAITING_FOR_RESOURCE.value,
                    blocker="BOUNDED_LIVE_NOT_ENABLED",
                )
                return self._persist(job)
            if self.knowledge_ledger is not None:
                execution_context_preflight(
                    self.knowledge_ledger,
                    project_id="AOS",
                    task_class="PLATFORM_RUNTIME_REPAIR",
                    module_ids=[str(finding.get("component") or "PlatformRecovery")],
                    paths=list(proposed.get("files_likely_affected") or ()),
                    base_sha=self.source_base_sha,
                )
            success, stage, repair = self.repair_engine.attempt_autonomous_repair(finding_id)
            job.update(
                disposition=(
                    RepairDisposition.REPAIRED_VERIFIED.value
                    if success
                    else RepairDisposition.FAILED_VERIFICATION.value
                ),
                repair_stage=stage,
                repair_record=repair,
            )
            if success and self.knowledge_ledger is not None:
                common = {
                    "project_id": "AOS",
                    "agent_class": "AOS_NATIVE",
                    "tool_name": "aos.platform_recovery",
                    "base_sha": self.source_base_sha,
                    "result_sha": self.source_base_sha,
                    "module_ids": [str(finding.get("component") or "PlatformRecovery")],
                    "changed_paths": list(proposed.get("files_likely_affected") or ()),
                }
                record_implementation_receipt(
                    self.knowledge_ledger,
                    idempotency_key=f"runtime-repair:{job_id}:{stage}",
                    runtime_evidence={"repair_stage": stage, "repair_record": repair},
                    claims={"finding_id": finding_id, "runtime_repair": True},
                    **common,
                )
                record_verification_receipt(
                    self.knowledge_ledger,
                    idempotency_key=f"runtime-repair-verification:{job_id}:{stage}",
                    verification={"status": "PASS", "repair_stage": stage},
                    claims={"finding_id": finding_id, "postcondition_verified": True},
                    **common,
                )
            return self._persist(job)

        job.update(disposition=RepairDisposition.HUMAN_REQUIRED.value)
        return self._persist(job)
