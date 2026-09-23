"""AOS Parallel Supervisor (R3).

Manages concurrent execution of independent runs with leases, heartbeat monitoring,
workspace locks, collision detection, and journal recovery.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any
import datetime
import time
import threading
import os
from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    RunIdentity,
    RunStatus,
    InvalidStateTransitionError,
)
from extensions.autonomy_fabric.antigravity_adapter import BaseAntigravityAdapter, FakeAntigravityAdapter
from extensions.autonomy_fabric.execution_backend import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    EvidenceClass,
    AgenticSessionIdentity,
)


def derive_run_capabilities(run_type: str, payload: Optional[Dict[str, Any]] = None) -> List[ExecutionCapability]:
    """Derives required execution capabilities dynamically from run_type consistent with PersistentCoordinator."""
    payload = payload or {}
    rt = (run_type or "").upper()
    if rt in ("PLAN", "REASONING", "MODEL_REASONING"):
        return [ExecutionCapability.MODEL_REASONING]
    if rt in ("PATCH", "FILE_WRITE", "DEV"):
        return [ExecutionCapability.FILE_WRITE] if payload.get("action") == "write_file" else [ExecutionCapability.PATCH_APPLY]
    if rt in ("GIT", "GIT_WRITE", "GIT_COMMIT", "GIT_BRANCH"):
        return [ExecutionCapability.GIT_WRITE]
    if rt in ("GIT_READ",):
        return [ExecutionCapability.GIT_READ]
    if rt in ("BROWSER", "VISUAL"):
        return [ExecutionCapability.BROWSER]
    if rt in ("CI", "CI_OBSERVE"):
        return [ExecutionCapability.CI_OBSERVE]
    if rt in ("AGENTIC", "ANTIGRAVITY", "CODEX"):
        return [ExecutionCapability.LONG_HORIZON_AGENTIC_WORK]
    return [ExecutionCapability.PROCESS_EXEC]


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
        adapter: Optional[Any] = None,
        max_concurrent_active_runs: int = 4,
        heartbeat_timeout_seconds: float = 60.0,
        router: Optional[Any] = None,
        allow_fake_adapter: bool = True,
    ):
        self.registry = registry
        self.router = router
        if not adapter and not router:
            if not allow_fake_adapter:
                raise RunSupervisorError("Production/runtime construction requires an eligible router or execution backend. Fake adapter not permitted.")
            self.adapter = FakeAntigravityAdapter()
        else:
            self.adapter = adapter
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

    @staticmethod
    def _agentic_identity(run: RunIdentity) -> Optional[AgenticSessionIdentity]:
        if not all((
            run.resource_id,
            run.backend_id,
            run.session_or_thread_id,
            run.workspace_fingerprint,
            run.source_sha,
            run.checkpoint_id,
        )):
            return None
        return AgenticSessionIdentity(
            resource_id=str(run.resource_id),
            backend_id=str(run.backend_id),
            session_or_thread_id=str(run.session_or_thread_id),
            workspace_fingerprint=str(run.workspace_fingerprint),
            source_sha=str(run.source_sha),
            checkpoint_id=str(run.checkpoint_id),
            last_successful_turn=int(run.last_successful_turn),
            last_successful_artifact=run.last_successful_artifact,
            started_at=run.started_at or run.created_at,
            updated_at=run.updated_at,
            adapter_contract_version=run.adapter_contract_version or "1.0.0",
            backend_version=run.backend_version,
            executable_sha256=run.executable_sha256,
            auth_mode=run.auth_mode,
            objective_id=run.objective_id,
            last_terminal_event=run.last_terminal_event,
            completed_work_unit_ids=list(run.completed_work_unit_ids),
            completed_work_unit_signatures=dict(run.completed_work_unit_signatures),
            artifact_hashes=dict(run.artifact_hashes),
            superseded_session_ids=list(run.superseded_session_ids),
        )

    def _accept_execution_result(
        self, run: RunIdentity, result: ExecutionResult, phase: str
    ) -> None:
        if result.agentic_identity is not None and result.status == "SUCCESS":
            identity = result.agentic_identity
            self.registry.update_run_metadata(run.run_id, {
                "resource_id": identity.resource_id,
                "backend_id": identity.backend_id,
                "adapter_contract_version": identity.adapter_contract_version,
                "backend_version": identity.backend_version,
                "executable_sha256": identity.executable_sha256,
                "auth_mode": identity.auth_mode,
                "objective_id": identity.objective_id,
            })
            self.registry.record_agentic_checkpoint(
                run.run_id,
                session_or_thread_id=str(identity.session_or_thread_id),
                workspace_fingerprint=identity.workspace_fingerprint,
                source_sha=identity.source_sha,
                checkpoint_id=identity.checkpoint_id,
                last_successful_turn=identity.last_successful_turn,
                completed_work_unit_ids=identity.completed_work_unit_ids,
                completed_work_unit_signatures=identity.completed_work_unit_signatures,
                artifact_hashes=identity.artifact_hashes,
                last_successful_artifact=identity.last_successful_artifact,
                last_terminal_event=identity.last_terminal_event or "turn.completed",
            )
        if result.status == "SUCCESS":
            self.registry.transition(run.run_id, RunStatus.COMPLETED, phase=phase)
            return
        if result.status in {"DEGRADED", "WAITING"}:
            stale = any(
                str(error).startswith("STALE_AGENT_SESSION:")
                for error in result.sanitized_errors
            )
            if stale and run.session_or_thread_id:
                self.registry.supersede_agentic_session(
                    run.run_id,
                    session_id=run.session_or_thread_id,
                    reason="SUPERSEDED_STALE_WORKSPACE",
                )
            self.registry.transition(
                run.run_id, RunStatus.WAITING_AGENT, phase=f"{phase}_REROUTE"
            )
            return
        self.registry.transition(run.run_id, RunStatus.FAILED, phase=phase)

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
        execution_payload: Optional[Dict[str, Any]] = None,
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
                source_sha=str((execution_payload or {}).get("source_sha") or "") or None,
                checkpoint_id=str((execution_payload or {}).get("checkpoint_id") or "") or None,
                objective_id=str((execution_payload or {}).get("objective_id") or "") or None,
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

            if initial_prompt or execution_payload:
                self.registry.transition(run.run_id, RunStatus.RUNNING, phase="PROMPT_PREPARATION")
                payload = dict(execution_payload or {})
                if initial_prompt:
                    payload.setdefault("prompt", initial_prompt)
                caps = derive_run_capabilities(run_type, payload)
                if self.router:
                    req = ExecutionRequest(
                        task_id=run.run_id,
                        project_id=project_id,
                        workspace=workspace_path or os.getcwd(),
                        operation_class=run_type,
                        required_capabilities=caps,
                        authority_id=authority_id,
                        payload=payload,
                        agentic_identity=self._agentic_identity(run),
                    )
                    exec_res = self.router.execute_with_failover(req)
                    self._accept_execution_result(run, exec_res, "EXECUTING_PROMPT")
                elif hasattr(self.adapter, "execute") and not hasattr(self.adapter, "execute_prompt"):
                    req = ExecutionRequest(
                        task_id=run.run_id,
                        project_id=project_id,
                        workspace=workspace_path or os.getcwd(),
                        operation_class=run_type,
                        required_capabilities=caps,
                        authority_id=authority_id,
                        payload=payload,
                        agentic_identity=self._agentic_identity(run),
                    )
                    exec_res = self.adapter.execute(req)
                    self._accept_execution_result(run, exec_res, "EXECUTING_PROMPT")
                else:
                    resp = self.adapter.execute_prompt(
                        prompt=initial_prompt or "",
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

                caps = derive_run_capabilities(run.run_type, {"prompt": prompt})
                if self.router:
                    req = ExecutionRequest(
                        task_id=run_id,
                        project_id=run.project_id,
                        workspace=run.workspace_path or os.getcwd(),
                        operation_class=run.run_type,
                        required_capabilities=caps,
                        authority_id=run.authority_id,
                        payload={"prompt": prompt, "source_sha": run.source_sha or run.base_sha},
                        agentic_identity=self._agentic_identity(run),
                    )
                    exec_res = self.router.execute_with_failover(req)
                    self._accept_execution_result(run, exec_res, "RESUMED_EXECUTION")
                elif hasattr(self.adapter, "execute") and not hasattr(self.adapter, "execute_prompt"):
                    req = ExecutionRequest(
                        task_id=run_id,
                        project_id=run.project_id,
                        workspace=run.workspace_path or os.getcwd(),
                        operation_class=run.run_type,
                        required_capabilities=caps,
                        authority_id=run.authority_id,
                        payload={"prompt": prompt, "source_sha": run.source_sha or run.base_sha},
                        agentic_identity=self._agentic_identity(run),
                    )
                    exec_res = self.adapter.execute(req)
                    self._accept_execution_result(run, exec_res, "RESUMED_EXECUTION")
                else:
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
