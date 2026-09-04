"""AOS Agent Run Registry (R1).

Defines first-class Run identity, explicit state machine, and append-only event journal.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Dict, List, Optional, Any
import datetime
import json
import uuid


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING_AGENT = "WAITING_AGENT"
    WAITING_AUTHORITY = "WAITING_AUTHORITY"
    WAITING_HUMAN = "WAITING_HUMAN"
    HOLD = "HOLD"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"


# Valid state transitions matrix (fail-closed rule)
VALID_TRANSITIONS: Dict[RunStatus, List[RunStatus]] = {
    RunStatus.QUEUED: [RunStatus.STARTING, RunStatus.CANCELED],
    RunStatus.STARTING: [RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.INTERRUPTED],
    RunStatus.RUNNING: [
        RunStatus.RUNNING,
        RunStatus.WAITING_AGENT,
        RunStatus.WAITING_AUTHORITY,
        RunStatus.WAITING_HUMAN,
        RunStatus.HOLD,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.WAITING_AGENT: [
        RunStatus.RUNNING,
        RunStatus.WAITING_AUTHORITY,
        RunStatus.WAITING_HUMAN,
        RunStatus.HOLD,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.WAITING_AUTHORITY: [
        RunStatus.RUNNING,
        RunStatus.WAITING_HUMAN,
        RunStatus.HOLD,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.WAITING_HUMAN: [
        RunStatus.RUNNING,
        RunStatus.HOLD,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.HOLD: [
        RunStatus.RUNNING,
        RunStatus.WAITING_AGENT,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.COMPLETED: [],
    RunStatus.FAILED: [RunStatus.QUEUED],  # Re-queue on explicit retry
    RunStatus.CANCELED: [],
    RunStatus.INTERRUPTED: [RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.FAILED],
}


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal state transition is attempted."""
    pass


@dataclass
class RunIdentity:
    run_id: str
    project_id: str
    run_type: str
    authority_id: str
    controller_id: str
    agent_provider: str
    agent_conversation_id: Optional[str] = None
    workspace_path: Optional[str] = None
    repository: Optional[str] = None
    base_sha: Optional[str] = None
    branch: Optional[str] = None
    parent_run_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    current_phase: str = "INITIALIZED"
    status: RunStatus = RunStatus.QUEUED
    human_input_required: bool = False
    authority_required: bool = False
    last_evidence_id: Optional[str] = None
    result_artifact: Optional[str] = None
    worker_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunIdentity":
        data_copy = dict(data)
        if isinstance(data_copy.get("status"), str):
            data_copy["status"] = RunStatus(data_copy["status"])
        return cls(**data_copy)


@dataclass
class RunEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    event_type: str = "STATE_CHANGE"
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)


class RunJournal:
    """Append-only run event journal."""

    def __init__(self):
        self._events: List[RunEvent] = []

    def append(self, event: RunEvent) -> None:
        self._events.append(event)

    def get_events_for_run(self, run_id: str) -> List[RunEvent]:
        return [e for e in self._events if e.run_id == run_id]

    def replay_run(self, initial_identity: RunIdentity, run_id: str) -> RunIdentity:
        """Derives current run state by replaying append-only event journal history."""
        run = RunIdentity.from_dict(initial_identity.to_dict())
        events = self.get_events_for_run(run_id)
        for ev in events:
            if ev.to_status:
                run.status = RunStatus(ev.to_status)
            if "current_phase" in ev.payload:
                run.current_phase = ev.payload["current_phase"]
            if "agent_conversation_id" in ev.payload:
                run.agent_conversation_id = ev.payload["agent_conversation_id"]
            if "worker_id" in ev.payload:
                run.worker_id = ev.payload["worker_id"]
            if "human_input_required" in ev.payload:
                run.human_input_required = ev.payload["human_input_required"]
            if "authority_required" in ev.payload:
                run.authority_required = ev.payload["authority_required"]
            if "last_evidence_id" in ev.payload:
                run.last_evidence_id = ev.payload["last_evidence_id"]
            if "result_artifact" in ev.payload:
                run.result_artifact = ev.payload["result_artifact"]
            if "started_at" in ev.payload:
                run.started_at = ev.payload["started_at"]
            if "completed_at" in ev.payload:
                run.completed_at = ev.payload["completed_at"]
            run.updated_at = ev.timestamp
        return run


class AgentRunRegistry:
    """Foundational AOS Agent Run Registry."""

    def __init__(self):
        self._runs: Dict[str, RunIdentity] = {}
        self.journal = RunJournal()

    def create_run(
        self,
        project_id: str,
        run_type: str,
        authority_id: str,
        controller_id: str,
        agent_provider: str,
        run_id: Optional[str] = None,
        workspace_path: Optional[str] = None,
        repository: Optional[str] = None,
        base_sha: Optional[str] = None,
        branch: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> RunIdentity:
        rid = run_id or f"run-{uuid.uuid4().hex[:12]}"
        identity = RunIdentity(
            run_id=rid,
            project_id=project_id,
            run_type=run_type,
            authority_id=authority_id,
            controller_id=controller_id,
            agent_provider=agent_provider,
            workspace_path=workspace_path,
            repository=repository,
            base_sha=base_sha,
            branch=branch,
            parent_run_id=parent_run_id,
            worker_id=worker_id,
            status=RunStatus.QUEUED,
        )
        self._runs[rid] = identity
        ev = RunEvent(
            run_id=rid,
            event_type="RUN_CREATED",
            from_status=None,
            to_status=RunStatus.QUEUED.value,
            payload=identity.to_dict(),
        )
        self.journal.append(ev)
        return identity

    def transition(
        self,
        run_id: str,
        new_status: RunStatus,
        phase: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> RunIdentity:
        if run_id not in self._runs:
            raise KeyError(f"Run {run_id} not found in registry")
        
        current_run = self._runs[run_id]
        current_status = current_run.status

        # Validate transition rules
        allowed = VALID_TRANSITIONS.get(current_status, [])
        if new_status not in allowed:
            raise InvalidStateTransitionError(
                f"Illegal transition for run {run_id}: {current_status.value} -> {new_status.value}. "
                f"Allowed transitions from {current_status.value}: {[s.value for s in allowed]}"
            )

        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        current_run.status = new_status
        current_run.updated_at = now_str

        ev_payload = dict(payload or {})
        if phase:
            current_run.current_phase = phase
            ev_payload["current_phase"] = phase

        if new_status == RunStatus.RUNNING and not current_run.started_at:
            current_run.started_at = now_str
            ev_payload["started_at"] = now_str
        elif new_status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED):
            current_run.completed_at = now_str
            ev_payload["completed_at"] = now_str

        if new_status == RunStatus.WAITING_HUMAN:
            current_run.human_input_required = True
            ev_payload["human_input_required"] = True
        elif new_status == RunStatus.WAITING_AUTHORITY:
            current_run.authority_required = True
            ev_payload["authority_required"] = True

        ev = RunEvent(
            run_id=run_id,
            timestamp=now_str,
            event_type="STATE_TRANSITION",
            from_status=current_status.value,
            to_status=new_status.value,
            payload=ev_payload,
        )
        self.journal.append(ev)
        return current_run

    def update_run_metadata(self, run_id: str, updates: Dict[str, Any]) -> RunIdentity:
        if run_id not in self._runs:
            raise KeyError(f"Run {run_id} not found")
        run = self._runs[run_id]
        for k, v in updates.items():
            if hasattr(run, k):
                setattr(run, k, v)
        run.updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ev = RunEvent(
            run_id=run_id,
            event_type="METADATA_UPDATE",
            from_status=run.status.value,
            to_status=run.status.value,
            payload=updates,
        )
        self.journal.append(ev)
        return run

    def get_run(self, run_id: str) -> Optional[RunIdentity]:
        return self._runs.get(run_id)

    def list_runs(self, project_id: Optional[str] = None, status: Optional[RunStatus] = None) -> List[RunIdentity]:
        results = list(self._runs.values())
        if project_id:
            results = [r for r in results if r.project_id == project_id]
        if status:
            results = [r for r in results if r.status == status]
        return results

    def rebuild_run(self, run_id: str) -> Optional[RunIdentity]:
        """Rebuilds run identity strictly from the append-only journal."""
        if run_id not in self._runs:
            return None
        return self.journal.replay_run(self._runs[run_id], run_id)
