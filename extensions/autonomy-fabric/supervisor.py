"""AOS Parallel Supervisor (R3).

Manages concurrent execution of independent runs with leases, heartbeat monitoring,
workspace locks, collision detection, and journal recovery.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any
import datetime
import time
import threading
from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    RunIdentity,
    RunStatus,
    InvalidStateTransitionError,
)
from extensions.autonomy_fabric.antigravity_adapter import BaseAntigravityAdapter, FakeAntigravityAdapter


class RunSupervisorError(ValueError):
    """Raised when supervisor rules or bounds are violated."""
    pass


class WorkspaceCollisionError(RunSupervisorError):
    """Raised when a workspace path is concurrently claimed."""
    pass


class BranchCollisionError(RunSupervisorError):
    """Raised when a git branch is concurrently claimed."""
    pass


class ConcurrencyLimitError(RunSupervisorError):
    """Raised when max concurrent active runs is exceeded."""
    pass


@dataclass
class RunLease:
    run_id: str
    owner_id: str
    workspace_path: Optional[str]
    branch: Optional[str]
    acquired_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    last_heartbeat: float = field(default_factory=time.time)
    lease_ttl_seconds: float = 60.0

    def is_expired(self, current_time: Optional[float] = None) -> bool:
        now = current_time or time.time()
        return (now - self.last_heartbeat) > self.lease_ttl_seconds

    def touch(self):
        self.last_heartbeat = time.time()


class ParallelSupervisor:
    """Manages bounded concurrent runs without state conflation."""

    def __init__(
        self,
        registry: AgentRunRegistry,
        adapter: Optional[BaseAntigravityAdapter] = None,
        max_concurrent_active_runs: int = 4,
        heartbeat_timeout_seconds: float = 60.0,
    ):
        self.registry = registry
        self.adapter = adapter or FakeAntigravityAdapter()
        self.max_concurrent_active_runs = max_concurrent_active_runs
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        
        self.leases: Dict[str, RunLease] = {}
        self.workspace_locks: Dict[str, str] = {}  # workspace_path -> run_id
        self.branch_locks: Dict[str, str] = {}     # branch -> run_id
        self._lock = threading.RLock()

    def get_active_count(self) -> int:
        active_statuses = {RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_AGENT}
        runs = self.registry.list_runs()
        return sum(1 for r in runs if r.status in active_statuses)

    def launch_run(
        self,
        project_id: str,
        run_type: str,
        authority_id: str,
        controller_id: str,
        agent_provider: str,
        workspace_path: Optional[str] = None,
        branch: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> RunIdentity:
        with self._lock:
            # Check concurrency limit
            if self.get_active_count() >= self.max_concurrent_active_runs:
                raise ConcurrencyLimitError(
                    f"Max concurrent active runs reached ({self.max_concurrent_active_runs})"
                )

            # Check workspace collision
            if workspace_path and workspace_path in self.workspace_locks:
                existing_run = self.workspace_locks[workspace_path]
                raise WorkspaceCollisionError(
                    f"Workspace path {workspace_path} already locked by run {existing_run}"
                )

            # Check branch collision
            if branch and branch in self.branch_locks:
                existing_run = self.branch_locks[branch]
                raise BranchCollisionError(
                    f"Branch {branch} already locked by run {existing_run}"
                )

            run = self.registry.create_run(
                project_id=project_id,
                run_type=run_type,
                authority_id=authority_id,
                controller_id=controller_id,
                agent_provider=agent_provider,
                workspace_path=workspace_path,
                branch=branch,
                parent_run_id=parent_run_id,
                worker_id=worker_id,
            )

            # Acquire locks & lease
            if workspace_path:
                self.workspace_locks[workspace_path] = run.run_id
            if branch:
                self.branch_locks[branch] = run.run_id

            lease = RunLease(
                run_id=run.run_id,
                owner_id=controller_id,
                workspace_path=workspace_path,
                branch=branch,
                lease_ttl_seconds=self.heartbeat_timeout_seconds,
            )
            self.leases[run.run_id] = lease

            # Transition to STARTING
            self.registry.transition(run.run_id, RunStatus.STARTING, phase="LAUNCH")

            if initial_prompt:
                self.registry.transition(run.run_id, RunStatus.RUNNING, phase="PROMPT_PREPARATION")
                resp = self.adapter.execute_prompt(
                    prompt=initial_prompt,
                    workspace_path=workspace_path,
                )
                self.registry.update_run_metadata(
                    run.run_id, {"agent_conversation_id": resp.conversation_id}
                )
                if resp.mapped_aos_status != RunStatus.RUNNING:
                    self.registry.transition(run.run_id, resp.mapped_aos_status, phase="EXECUTING_PROMPT")

            return self.registry.get_run(run.run_id)  # type: ignore

    def heartbeat(self, run_id: str) -> None:
        with self._lock:
            if run_id in self.leases:
                self.leases[run_id].touch()
            else:
                run = self.registry.get_run(run_id)
                if run and run.status in (RunStatus.RUNNING, RunStatus.STARTING, RunStatus.WAITING_AGENT):
                    lease = RunLease(
                        run_id=run_id,
                        owner_id=run.controller_id,
                        workspace_path=run.workspace_path,
                        branch=run.branch,
                        lease_ttl_seconds=self.heartbeat_timeout_seconds,
                    )
                    self.leases[run_id] = lease

    def reconcile_stale_runs(self) -> List[str]:
        """Detects stale runs whose lease has expired and marks them INTERRUPTED or FAILED."""
        stale_ids = []
        now = time.time()
        with self._lock:
            for rid, lease in list(self.leases.items()):
                run = self.registry.get_run(rid)
                if not run:
                    continue
                if run.status in (RunStatus.RUNNING, RunStatus.STARTING, RunStatus.WAITING_AGENT):
                    if lease.is_expired(now):
                        stale_ids.append(rid)
                        self.registry.transition(
                            rid, RunStatus.INTERRUPTED, phase="STALE_LEASE_TIMEOUT"
                        )
                        self._release_locks(rid)
        return stale_ids

    def resume_run(self, run_id: str, prompt: str) -> RunIdentity:
        with self._lock:
            run = self.registry.get_run(run_id)
            if not run:
                raise KeyError(f"Run {run_id} not found")

            if run.status in (RunStatus.INTERRUPTED, RunStatus.WAITING_AGENT, RunStatus.RUNNING, RunStatus.HOLD):
                # Transition back to RUNNING if allowed
                if run.status != RunStatus.RUNNING:
                    self.registry.transition(run_id, RunStatus.RUNNING, phase="RESUME")

                resp = self.adapter.execute_prompt(
                    prompt=prompt,
                    conversation_id=run.agent_conversation_id,
                    workspace_path=run.workspace_path,
                )

                if resp.conversation_id:
                    self.registry.update_run_metadata(
                        run_id, {"agent_conversation_id": resp.conversation_id}
                    )

                if resp.mapped_aos_status != RunStatus.RUNNING:
                    self.registry.transition(run_id, resp.mapped_aos_status, phase="RESUMED_EXECUTION")
                self.heartbeat(run_id)
                return self.registry.get_run(run_id) # type: ignore
            else:
                raise RunSupervisorError(f"Cannot resume run {run_id} from status {run.status.value}")

    def interrupt_run(self, run_id: str, reason: str = "Supervisor requested interrupt") -> RunIdentity:
        with self._lock:
            run = self.registry.get_run(run_id)
            if not run:
                raise KeyError(f"Run {run_id} not found")

            if run.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED):
                self.registry.transition(
                    run_id, RunStatus.INTERRUPTED, phase="INTERRUPTED", payload={"reason": reason}
                )
                self._release_locks(run_id)
            return self.registry.get_run(run_id) # type: ignore

    def recover_from_crash(self) -> int:
        """Rebuilds locks and reconciles run states after a supervisor crash."""
        with self._lock:
            self.leases.clear()
            self.workspace_locks.clear()
            self.branch_locks.clear()

            runs = self.registry.list_runs()
            recovered_count = 0
            for run in runs:
                # Rebuild state from journal history
                rebuilt = self.registry.rebuild_run(run.run_id)
                if not rebuilt:
                    continue

                if rebuilt.status in (RunStatus.STARTING, RunStatus.RUNNING, RunStatus.WAITING_AGENT):
                    # Check for locks
                    if rebuilt.workspace_path:
                        self.workspace_locks[rebuilt.workspace_path] = rebuilt.run_id
                    if rebuilt.branch:
                        self.branch_locks[rebuilt.branch] = rebuilt.run_id
                    
                    self.leases[rebuilt.run_id] = RunLease(
                        run_id=rebuilt.run_id,
                        owner_id=rebuilt.controller_id,
                        workspace_path=rebuilt.workspace_path,
                        branch=rebuilt.branch,
                        lease_ttl_seconds=self.heartbeat_timeout_seconds,
                    )
                    recovered_count += 1

            return recovered_count

    def _release_locks(self, run_id: str):
        if run_id in self.leases:
            lease = self.leases.pop(run_id)
            if lease.workspace_path and self.workspace_locks.get(lease.workspace_path) == run_id:
                del self.workspace_locks[lease.workspace_path]
            if lease.branch and self.branch_locks.get(lease.branch) == run_id:
                del self.branch_locks[lease.branch]
