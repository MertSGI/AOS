"""AOS Evidence / Report Aggregator (R7).

Indexes per-run evidence with strict separation of claims, observations,
verifications, and controller dispositions. Generates structured project summaries.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import datetime
import uuid
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus, RunIdentity


class EvidenceType(str, Enum):
    CLAIM = "CLAIM"
    OBSERVATION = "OBSERVATION"
    VERIFICATION = "VERIFICATION"
    CONTROLLER_DISPOSITION = "CONTROLLER_DISPOSITION"


@dataclass
class EvidenceRecord:
    evidence_id: str
    run_id: str
    phase: str
    project_id: str
    git_sha: Optional[str]
    evidence_type: EvidenceType
    producer: str  # e.g., "executor:agent-1", "controller:supervisor", "verifier:pytest"
    title: str
    payload: Dict[str, Any] = field(default_factory=dict)
    artifact_path: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence_type"] = self.evidence_type.value
        return d


@dataclass
class ProjectSummary:
    project_id: str
    timestamp: str
    running_runs: List[str]
    waiting_runs: List[str]
    failed_runs: List[str]
    human_input_required_runs: List[str]
    completed_runs: List[str]
    consumed_authorities: List[str]
    next_safe_action: str
    total_runs: int
    progress_percentage: float


class EvidenceAggregator:
    """Per-run evidence indexer and project summary generator."""

    def __init__(self, registry: AgentRunRegistry):
        self.registry = registry
        self._evidence: Dict[str, EvidenceRecord] = {}

    def record_evidence(
        self,
        run_id: str,
        phase: str,
        project_id: str,
        evidence_type: EvidenceType,
        producer: str,
        title: str,
        payload: Optional[Dict[str, Any]] = None,
        git_sha: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ) -> EvidenceRecord:
        eid = f"ev-{uuid.uuid4().hex[:12]}"
        record = EvidenceRecord(
            evidence_id=eid,
            run_id=run_id,
            phase=phase,
            project_id=project_id,
            git_sha=git_sha,
            evidence_type=evidence_type,
            producer=producer,
            title=title,
            payload=payload or {},
            artifact_path=artifact_path,
        )
        self._evidence[eid] = record

        # Link last evidence ID on run
        run = self.registry.get_run(run_id)
        if run:
            self.registry.update_run_metadata(run_id, {"last_evidence_id": eid})

        return record

    def list_evidence(
        self,
        run_id: Optional[str] = None,
        project_id: Optional[str] = None,
        evidence_type: Optional[EvidenceType] = None,
    ) -> List[EvidenceRecord]:
        records = list(self._evidence.values())
        if run_id:
            records = [r for r in records if r.run_id == run_id]
        if project_id:
            records = [r for r in records if r.project_id == project_id]
        if evidence_type:
            records = [r for r in records if r.evidence_type == evidence_type]
        return records

    def generate_project_summary(self, project_id: str) -> ProjectSummary:
        runs = self.registry.list_runs(project_id=project_id)
        
        running = [r.run_id for r in runs if r.status in (RunStatus.RUNNING, RunStatus.STARTING)]
        waiting = [r.run_id for r in runs if r.status in (RunStatus.WAITING_AGENT, RunStatus.WAITING_AUTHORITY)]
        failed = [r.run_id for r in runs if r.status == RunStatus.FAILED]
        human_req = [r.run_id for r in runs if r.status == RunStatus.WAITING_HUMAN or r.human_input_required]
        completed = [r.run_id for r in runs if r.status == RunStatus.COMPLETED]
        authorities = list({r.authority_id for r in runs if r.authority_id})

        total = len(runs)
        progress = round((len(completed) / total * 100.0), 2) if total > 0 else 0.0

        if human_req:
            next_action = f"Review human-gated run(s): {', '.join(human_req)}"
        elif failed:
            next_action = f"Investigate failed run(s): {', '.join(failed)}"
        elif running or waiting:
            next_action = "Observe active run progress"
        else:
            next_action = "Project completed cleanly. Ready for controller review."

        return ProjectSummary(
            project_id=project_id,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            running_runs=running,
            waiting_runs=waiting,
            failed_runs=failed,
            human_input_required_runs=human_req,
            completed_runs=completed,
            consumed_authorities=authorities,
            next_safe_action=next_action,
            total_runs=total,
            progress_percentage=progress,
        )
