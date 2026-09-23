"""AOS Agent Run Registry (R1 / Correction R1).

Defines first-class Run identity, explicit state machine, append-only event journals
(in-memory and durable file-backed JSONL journal), and journal-based process restart recovery.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any
import datetime
import json
import uuid
import os


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
        RunStatus.WAITING_AGENT,
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
        RunStatus.WAITING_AUTHORITY,
        RunStatus.RUNNING,
        RunStatus.WAITING_HUMAN,
        RunStatus.HOLD,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.WAITING_HUMAN: [
        RunStatus.WAITING_HUMAN,
        RunStatus.RUNNING,
        RunStatus.HOLD,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELED,
        RunStatus.INTERRUPTED,
    ],
    RunStatus.HOLD: [
        RunStatus.HOLD,
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


class JournalCorruptRecordError(ValueError):
    """Raised when a corrupt or truncated record is found in the durable journal."""
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
    resource_id: Optional[str] = None
    backend_id: Optional[str] = None
    session_or_thread_id: Optional[str] = None
    workspace_fingerprint: Optional[str] = None
    source_sha: Optional[str] = None
    checkpoint_id: Optional[str] = None
    last_successful_turn: int = 0
    last_successful_artifact: Optional[Dict[str, str]] = None
    adapter_contract_version: Optional[str] = None
    backend_version: Optional[str] = None
    executable_sha256: Optional[str] = None
    auth_mode: Optional[str] = None
    objective_id: Optional[str] = None
    last_terminal_event: Optional[str] = None
    completed_work_unit_ids: List[str] = field(default_factory=list)
    completed_work_unit_signatures: Dict[str, str] = field(default_factory=dict)
    artifact_hashes: Dict[str, str] = field(default_factory=dict)
    superseded_session_ids: List[str] = field(default_factory=list)

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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunEvent":
        if not isinstance(data, dict) or "event_id" not in data or "run_id" not in data:
            raise JournalCorruptRecordError("Invalid or missing required fields in RunEvent record")
        return cls(**data)


class RunJournal:
    """In-memory run event journal."""

    def __init__(self):
        self._events: List[RunEvent] = []

    def append(self, event: RunEvent) -> None:
        self._events.append(event)

    def get_events_for_run(self, run_id: str) -> List[RunEvent]:
        return [e for e in self._events if e.run_id == run_id]

    def list_all_events(self) -> List[RunEvent]:
        return list(self._events)

    def replay_run(self, initial_identity: RunIdentity, run_id: str) -> RunIdentity:
        """Derives current run state by replaying append-only event journal history."""
        run = RunIdentity.from_dict(initial_identity.to_dict())
        events = self.get_events_for_run(run_id)
        for ev in events:
            if ev.to_status:
                run.status = RunStatus(ev.to_status)
            _apply_event_payload(run, ev.payload)
            run.updated_at = ev.timestamp
        return run


_REPLAYABLE_FIELDS = {
    "current_phase", "agent_conversation_id", "worker_id",
    "human_input_required", "authority_required", "last_evidence_id",
    "result_artifact", "started_at", "completed_at", "resource_id",
    "backend_id", "session_or_thread_id", "workspace_fingerprint",
    "source_sha", "checkpoint_id", "last_successful_turn",
    "last_successful_artifact", "adapter_contract_version",
    "backend_version", "executable_sha256", "auth_mode", "objective_id",
    "last_terminal_event", "completed_work_unit_ids",
    "completed_work_unit_signatures", "artifact_hashes",
    "superseded_session_ids",
}


def _apply_event_payload(run: RunIdentity, payload: Dict[str, Any]) -> None:
    for key in _REPLAYABLE_FIELDS:
        if key in payload:
            value = payload[key]
            if isinstance(value, (dict, list)):
                value = value.copy()
            setattr(run, key, value)


class FileRunJournal(RunJournal):
    """File-backed durable JSONL append-only journal with explicit flush and corrupt record protection."""

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)
        self.reload()

    def append(self, event: RunEvent) -> None:
        super().append(event)
        line = json.dumps(event.to_dict()) + "\n"
        with open(self.file_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def reload(self) -> List[RunEvent]:
        self._events.clear()
        if not os.path.exists(self.file_path):
            return []

        with open(self.file_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    event = RunEvent.from_dict(data)
                    self._events.append(event)
                except Exception as ex:
                    raise JournalCorruptRecordError(
                        f"Corrupt or truncated record on line {line_no} in journal {self.file_path}: {ex}"
                    )
        return self._events


class AgentRunRegistry:
    """Foundational AOS Agent Run Registry."""

    def __init__(self, journal: Optional[RunJournal] = None):
        self._runs: Dict[str, RunIdentity] = {}
        self.journal = journal or RunJournal()

        # If loading from an existing non-empty journal, reconstruct runs
        if self.journal.list_all_events():
            self._reconstruct_all_from_journal()

    def _reconstruct_all_from_journal(self):
        for ev in self.journal.list_all_events():
            if ev.event_type == "RUN_CREATED":
                identity = RunIdentity.from_dict(ev.payload)
                self._runs[identity.run_id] = identity
            elif ev.run_id in self._runs:
                # Replay event onto existing run state
                run = self._runs[ev.run_id]
                if ev.to_status:
                    run.status = RunStatus(ev.to_status)
                _apply_event_payload(run, ev.payload)
                run.updated_at = ev.timestamp

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
        resource_id: Optional[str] = None,
        backend_id: Optional[str] = None,
        source_sha: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        objective_id: Optional[str] = None,
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
            resource_id=resource_id,
            backend_id=backend_id,
            source_sha=source_sha,
            checkpoint_id=checkpoint_id,
            objective_id=objective_id,
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

    def record_agentic_checkpoint(
        self,
        run_id: str,
        *,
        session_or_thread_id: str,
        workspace_fingerprint: str,
        source_sha: str,
        checkpoint_id: str,
        last_successful_turn: int,
        completed_work_unit_ids: List[str],
        completed_work_unit_signatures: Dict[str, str],
        artifact_hashes: Dict[str, str],
        last_successful_artifact: Optional[Dict[str, str]] = None,
        last_terminal_event: str = "turn.completed",
    ) -> RunIdentity:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        if run.status == RunStatus.COMPLETED:
            raise InvalidStateTransitionError("completed run cannot re-enter agentic execution")
        if last_successful_turn < run.last_successful_turn:
            raise ValueError("last_successful_turn cannot move backwards")
        prior_ids = set(run.completed_work_unit_ids)
        next_ids = set(completed_work_unit_ids)
        if not prior_ids.issubset(next_ids):
            raise ValueError("completed work cannot be removed from a checkpoint")
        return self.update_run_metadata(run_id, {
            "session_or_thread_id": session_or_thread_id,
            "agent_conversation_id": session_or_thread_id,
            "workspace_fingerprint": workspace_fingerprint,
            "source_sha": source_sha,
            "checkpoint_id": checkpoint_id,
            "last_successful_turn": int(last_successful_turn),
            "last_successful_artifact": dict(last_successful_artifact) if last_successful_artifact else None,
            "last_terminal_event": last_terminal_event,
            "completed_work_unit_ids": sorted(next_ids),
            "completed_work_unit_signatures": dict(completed_work_unit_signatures),
            "artifact_hashes": dict(artifact_hashes),
        })

    def supersede_agentic_session(
        self, run_id: str, *, session_id: str, reason: str
    ) -> RunIdentity:
        if reason != "SUPERSEDED_STALE_WORKSPACE":
            raise ValueError("agentic session supersession requires a typed reason")
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        superseded = list(run.superseded_session_ids)
        if session_id not in superseded:
            superseded.append(session_id)
        return self.update_run_metadata(run_id, {
            "superseded_session_ids": superseded,
            "session_or_thread_id": None,
            "agent_conversation_id": None,
            "last_terminal_event": reason,
        })

    def assert_work_unit_not_completed(
        self, run_id: str, work_unit_id: str, signature: Optional[str] = None
    ) -> None:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} not found")
        if work_unit_id in run.completed_work_unit_ids:
            raise InvalidStateTransitionError(f"completed work unit cannot be re-entered: {work_unit_id}")
        if signature and signature in set(run.completed_work_unit_signatures.values()):
            raise InvalidStateTransitionError("completed work signature cannot be re-entered")

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
