"""AOS V2 Durable Execution Engine.

Holds exclusive kernel owner lock, manages lifecycle schema 2 transitions,
validates immutable DAG plans, controls zero-paid resource budget, executes
bounded exact-edit mutations in sandbox, reconciles crashes, and closes admission atomically.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from aos.v2.acceptance import (
    FunctionOracleVerifier,
    OracleCriterion,
    OutboxExporter,
    VerificationResult,
)
from aos.v2.bridge import (
    GitBridgeError,
    PreparedCandidate,
    PublishedCommit,
    RecoveryDecision,
    TaskIntent,
    WorkerGitBridge,
    compute_file_sha256,
)
from aos.v2.lifecycle import (
    KernelOwnerLock,
    LifecycleStore,
    OwnerBusyError,
    OwnerIdentity,
    TrustedGrant,
)
from aos.v2.registry import (
    AuthorizedProject,
    RegistryAuthorizationError,
    RegistrySnapshot,
    authorize,
    load_registry,
    revalidate,
)
from aos.v2.resources import (
    ResourceBudgetExceededError,
    ResourceError,
    ResourceGateway,
    ResourceUnavailableError,
)
from aos.v2.sandbox import SandboxRunner, SandboxUnavailableError


logger = logging.getLogger("aos.v2.engine")


class EngineError(Exception):
    """Raised on engine error."""


class PlanValidationError(EngineError):
    """Raised when DAG plan is malformed, cyclic, or empty."""


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    target_rel_path: str
    criterion: OracleCriterion
    depends_on: Sequence[str] = field(default_factory=tuple)
    description: str = ""


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    tasks: Sequence[TaskNode]

    def validate(self, project: AuthorizedProject) -> None:
        """Validate that plan is a non-empty, acyclic, allowlisted DAG."""
        if not self.tasks:
            raise PlanValidationError("Plan cannot be empty (empty plan != success)")

        task_map = {}
        allowlisted = set(project.write_files)
        for t in self.tasks:
            if t.task_id in task_map:
                raise PlanValidationError(f"Duplicate task_id in plan: {t.task_id}")
            task_map[t.task_id] = t

            # Check target path is allowlisted
            norm_target = str(Path(t.target_rel_path)).replace("\\", "/")
            if norm_target not in allowlisted:
                raise PlanValidationError(
                    f"Task target path {t.target_rel_path} not in registry allowlisted source files"
                )

        # Validate DAG dependencies and detect cycles via topological sort
        visited = set()
        visiting = set()

        def dfs(task_id: str) -> None:
            if task_id in visiting:
                raise PlanValidationError(f"Cyclic dependency detected involving task: {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            node = task_map.get(task_id)
            if not node:
                raise PlanValidationError(f"Task depends on unknown task_id: {task_id}")
            for dep in node.depends_on:
                dfs(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for t in self.tasks:
            if t.task_id not in visited:
                dfs(t.task_id)


class Engine:
    """The single durable operational owner of AOS V2 execution."""

    def __init__(
        self,
        v2_home: Path | str,
        registry_path: Path | str,
        *,
        gateway: ResourceGateway | None = None,
        sandbox_runner: SandboxRunner | None = None,
    ) -> None:
        self.v2_home = Path(v2_home).resolve()
        marker = self.v2_home / ".aos-v2-home"
        if self.v2_home.exists() and any(self.v2_home.iterdir()) and not marker.exists():
            raise EngineError(f"Refusing to adopt non-empty unmarked directory as v2_home: {self.v2_home}")
        self.v2_home.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text("AOS_V2_JOURNAL_1\n")
        self.registry_path = Path(registry_path).resolve()
        self.snapshot = load_registry(self.registry_path)

        # Acquire exclusive process-lifetime kernel lock
        self.owner_lock = KernelOwnerLock(self.v2_home)
        self.owner_lock.acquire()

        # Initialize SQLite lifecycle (schema 2)
        self.lifecycle = LifecycleStore(self.v2_home)
        self.owner_epoch = self.lifecycle.increment_owner_epoch(self.owner_lock.identity)

        self.gateway = gateway or ResourceGateway(self.snapshot, self.lifecycle)
        self.sandbox = sandbox_runner or SandboxRunner()
        self.verifier = FunctionOracleVerifier(self.sandbox)
        self.outbox = OutboxExporter(self.v2_home)

    def close(self) -> None:
        """Release kernel lock and close resources."""
        self.owner_lock.release()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def admit(self, project_id: str, goal: str, idempotency_key: str) -> dict[str, Any]:
        """Atomically admit one command under frozen project contract."""
        self.snapshot = load_registry(self.registry_path)
        project = authorize(self.snapshot, project_id)
        revalidate(self.snapshot, project.binding())

        # Check existing command by (project_id, idempotency_key)
        existing = self.lifecycle.get_command_by_idempotency_key(idempotency_key, project_id=project_id)
        if existing:
            if existing["goal"] != goal:
                raise LifecycleConflict(f"Idempotency conflict: key '{idempotency_key}' already exists with different goal")
            return self.lifecycle.snapshot(existing["id"])

        command_id = f"cmd_{secrets.token_hex(16)}"
        workspace_root = Path(project.workspace)
        grant = TrustedGrant(
            project_id=project.project_id,
            authority_id=project.authority_id,
            workspace=str(workspace_root),
            base_sha=project.base_sha,
            write_files=project.write_files,
        )
        criteria_ids = tuple(c.id for c in project.criteria) if project.criteria else ("goal_admission_criterion",)
        contract_json = json.dumps({
            "contract": "AOS-V2-SEAM-1",
            "budget_max_attempts": 4,
            "capability_digest": project.capability_digest,
        })

        cid = self.lifecycle.admit(
            command_id=command_id,
            request_key=idempotency_key,
            goal=goal,
            criteria=criteria_ids,
            grant=grant,
            contract_json=contract_json,
            owner_epoch=self.owner_epoch,
        )

        # Check resource eligibility preflight
        eligible_profiles = self.gateway.get_eligible_profiles()
        if not eligible_profiles:
            # Durably admit WAITING_RESOURCE / HOLD
            snap = self.lifecycle.snapshot(cid)
            self.lifecycle.transition(cid, snap["revision"], "WAITING_RESOURCE", reason="WAITING_RESOURCE: no eligible resources")
            return self.lifecycle.snapshot(cid)

        # Admit into QUEUED state
        return self.lifecycle.snapshot(cid)

    def resume(self, command_id: str) -> dict[str, Any]:
        """Resume a held or paused command."""
        snap = self.lifecycle.snapshot(command_id)
        cur_state = snap["state"]
        if cur_state in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
            raise EngineError(f"Cannot resume terminal command in state {cur_state}")
        if cur_state not in ("PAUSED_SAFE", "WAITING_RESOURCE", "WAITING_AUTHORITY", "HUMAN_REQUIRED"):
            raise EngineError(f"Cannot resume command in state {cur_state}; must be PAUSED_SAFE or WAITING_*")

        # Revalidate project and resources
        self.snapshot = load_registry(self.registry_path)
        project = authorize(self.snapshot, snap["project_id"])
        revalidate(self.snapshot, project.binding())

        self.lifecycle.transition(command_id, snap["revision"], "QUEUED", reason="user_resume_request")
        return self.lifecycle.snapshot(command_id)

    def pause(self, command_id: str) -> dict[str, Any]:
        """Pause an active command."""
        snap = self.lifecycle.snapshot(command_id)
        cur_state = snap["state"]
        if cur_state in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
            raise EngineError(f"Cannot pause terminal command in state {cur_state}")
        if cur_state != "PAUSED_SAFE":
            self.lifecycle.transition(command_id, snap["revision"], "PAUSED_SAFE", reason="user_pause_request")
        return self.lifecycle.snapshot(command_id)

    def cancel(self, command_id: str) -> dict[str, Any]:
        """Cancel an active or held command."""
        snap = self.lifecycle.snapshot(command_id)
        cur_state = snap["state"]
        if cur_state in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
            raise EngineError(f"Cannot cancel terminal command in state {cur_state}")
        self.lifecycle.finalize(command_id, snap["revision"], "CANCELLED", criterion_receipts={})
        return self.lifecycle.snapshot(command_id)

    def recover(self, command_id: str) -> dict[str, Any]:
        """Perform read-only observation then allowed reconciliation for command."""
        snap = self.lifecycle.snapshot(command_id)
        project = authorize(self.snapshot, snap["project_id"])
        workspace_root = Path(project.workspace)
        bridge = WorkerGitBridge(workspace_root)

        # Reconcile tasks
        tasks = self.lifecycle.get_tasks_for_command(command_id)
        for t in tasks:
            task_id = t["task_id"]
            phase = t["phase"]
            decision = bridge.reconcile_task(t)
            logger.info(f"Recovery reconciliation for task {task_id} (phase {phase}): {decision.action}")

        return self.lifecycle.snapshot(command_id)

    def execute_goal(
        self,
        project_id: str,
        goal: str,
        plan: ExecutionPlan,
        *,
        idempotency_key: str,
        generator_func: Callable[[TaskNode, str, Path], Sequence[Mapping[str, str]]] | None = None,
    ) -> dict[str, Any]:
        """Execute complete goal through plan DAG to terminal CLOSED admission."""
        # 1. Authority Preflight
        try:
            # Refresh snapshot
            self.snapshot = load_registry(self.registry_path)
            project = authorize(self.snapshot, project_id)
        except RegistryAuthorizationError as e:
            raise EngineError(f"Registry authorization failed for {project_id}: {e}") from e

        # 2. Validate DAG Plan
        plan.validate(project)

        workspace_root = Path(project.workspace)

        # 3. Check Git head and construct TrustedGrant
        bridge = WorkerGitBridge(workspace_root)
        trusted_grant = TrustedGrant(
            project_id=project.project_id,
            authority_id=project.authority_id,
            workspace=str(workspace_root),
            base_sha=project.base_sha,
            write_files=project.write_files,
        )

        # 4. Admit command into LifecycleStore
        command_id = f"cmd_{idempotency_key.replace('-', '_')[:32]}"
        criteria_ids = tuple(t.criterion.criterion_id for t in plan.tasks)
        contract_json = json.dumps({"contract": "AOS-V2-SEAM-1", "budget_max_attempts": 4})

        cmd_id = self.lifecycle.admit(
            command_id,
            idempotency_key,
            goal,
            criteria_ids,
            trusted_grant,
            contract_json=contract_json,
            owner_epoch=self.owner_epoch,
        )

        snap = self.lifecycle.snapshot(cmd_id)
        cmd_row = snap
        if cmd_row["state"] in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "SUPERSEDED"):
            return cmd_row

        scratch_base = self.v2_home / "scratch" / cmd_id
        scratch_base.mkdir(parents=True, exist_ok=True)
        current_head = project.base_sha

        try:
            # Transition QUEUED -> PLANNING -> RUNNING
            rev = cmd_row["revision"]
            if cmd_row["state"] == "QUEUED":
                self.lifecycle.transition(cmd_id, rev, "PLANNING", reason="plan_admitted")
                rev += 1
            if cmd_row["state"] in ("QUEUED", "PLANNING"):
                self.lifecycle.transition(cmd_id, rev, "RUNNING", reason="execution_started")
                rev += 1

            # 5. Execute Tasks in Topological Order
            for task_node in plan.tasks:
                task_id = task_node.task_id
                target_path = workspace_root / task_node.target_rel_path
                pre_sha = compute_file_sha256(target_path)

                task_scratch = scratch_base / task_id
                task_scratch.mkdir(parents=True, exist_ok=True)

                # Begin task in lifecycle
                begin_status = self.lifecycle.begin_task(
                    cmd_id,
                    task_id,
                    pre_fingerprint=pre_sha,
                    write_files=(task_node.target_rel_path,),
                )
                if begin_status == "ALREADY_ACCEPTED":
                    logger.info(f"Task {task_id} already accepted; skipping")
                    continue

                # Reserve attempt
                self.lifecycle.record_attempt(
                    attempt_id=f"att_{secrets.token_hex(8)}",
                    command_id=cmd_id,
                    purpose="EDIT",
                    profile_id="simulated_profile",
                    request_sha=pre_sha,
                    task_id=task_id,
                )

                # Generate edits
                if not generator_func:
                    raise EngineError("No model generator provided for task execution")
                edits = generator_func(task_node, pre_sha, workspace_root)

                # Prepare candidate in private index
                current_branch = bridge.get_current_branch()
                intent = TaskIntent(
                    command_id=cmd_id,
                    task_id=task_id,
                    owner_epoch=self.owner_epoch,
                    repo_root=workspace_root,
                    owned_branch=current_branch,
                    expected_parent=current_head,
                    target_rel_path=task_node.target_rel_path,
                    pre_sha256=pre_sha,
                    edits=edits,
                )
                prepared = bridge.prepare_candidate(intent, task_scratch)
                self.lifecycle.update_task_phase(
                    cmd_id,
                    task_id,
                    "PREPARED",
                    fence=current_head,
                    prepared_tree=prepared.tree_sha,
                )

                # Verify candidate in sandbox
                verif_res = self.verifier.verify_criterion(
                    task_node.criterion,
                    prepared.candidate_dir,
                    task_scratch / "verif",
                )
                if not verif_res.passed:
                    raise EngineError(f"Task verification failed for {task_id}: {verif_res.error}")

                # Publish commit via CAS update-ref
                published = bridge.publish_candidate(prepared, message=f"AOS V2: {task_node.description}")
                current_head = published.commit_sha

                # Atomically materialize source
                bridge.materialize_source(prepared)

                # Accept task
                self.lifecycle.accept_task(
                    cmd_id,
                    task_id,
                    result_sha=published.commit_sha,
                    artifacts={task_node.target_rel_path: prepared.post_sha256},
                    verification_ref="sandbox_driver_v1",
                )
                logger.info(f"Task {task_id} successfully accepted: {published.commit_sha}")

            # 6. Transition RUNNING -> VERIFYING
            snap = self.lifecycle.snapshot(cmd_id)
            self.lifecycle.transition(cmd_id, snap["revision"], "VERIFYING", reason="tasks_complete")

            # 7. Final Goal Verification across whole repository
            criterion_receipts = {}
            for task_node in plan.tasks:
                final_verif = self.verifier.verify_criterion(
                    task_node.criterion,
                    workspace_root,
                    scratch_base / "final_verif",
                )
                if not final_verif.passed:
                    raise EngineError(f"Final repository verification failed for {task_node.task_id}: {final_verif.error}")
                criterion_receipts[task_node.criterion.criterion_id] = task_node.criterion.expected_oracle_sha

            # 8. Finalize Goal Success: Atomically close admission
            snap = self.lifecycle.snapshot(cmd_id)
            self.lifecycle.finalize(cmd_id, snap["revision"], "PROJECT_COMPLETE", criterion_receipts)

            # 9. Outbox Export
            result_payload = {
                "command_id": cmd_id,
                "project_id": project_id,
                "goal": goal,
                "state": "PROJECT_COMPLETE",
                "admission": "CLOSED",
                "commit_sha": current_head,
                "tasks": [t.task_id for t in plan.tasks],
            }
            self.outbox.export_result(
                cmd_id,
                result_payload,
                base_sha=trusted_grant.base_sha,
                result_sha=current_head,
                project_id=project_id,
            )

            final_snap = self.lifecycle.snapshot(cmd_id)
            return final_snap

        except (ResourceUnavailableError, SandboxUnavailableError) as e:
            logger.warning(f"Resource/Sandbox unavailable for command {cmd_id}: {e}")
            try:
                snap = self.lifecycle.snapshot(cmd_id)
                if snap["state"] not in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "HOLD"):
                    self.lifecycle.transition(cmd_id, snap["revision"], "HOLD", reason=f"WAITING_RESOURCE: {e}")
            except Exception as hold_err:
                logger.error(f"Error transitioning to HOLD: {hold_err}")
            raise
        except RegistryAuthorizationError as e:
            logger.warning(f"Authority unavailable for command {cmd_id}: {e}")
            try:
                snap = self.lifecycle.snapshot(cmd_id)
                if snap["state"] not in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "HOLD"):
                    self.lifecycle.transition(cmd_id, snap["revision"], "HOLD", reason=f"WAITING_AUTHORITY: {e}")
            except Exception as hold_err:
                logger.error(f"Error transitioning to HOLD: {hold_err}")
            raise
        except GitBridgeError as e:
            logger.warning(f"Git drift/conflict for command {cmd_id}: {e}")
            try:
                snap = self.lifecycle.snapshot(cmd_id)
                if snap["state"] not in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "HOLD"):
                    self.lifecycle.transition(cmd_id, snap["revision"], "HOLD", reason=f"HUMAN_REQUIRED: {e}")
            except Exception as hold_err:
                logger.error(f"Error transitioning to HOLD: {hold_err}")
            raise
        except Exception as e:
            logger.error(f"Goal execution failed: {e}", exc_info=True)
            try:
                snap = self.lifecycle.snapshot(cmd_id)
                if snap["state"] not in ("PROJECT_COMPLETE", "FAILED", "CANCELLED", "HOLD"):
                    self.lifecycle.finalize(cmd_id, snap["revision"], "FAILED", {})
                    result_payload = {
                        "command_id": cmd_id,
                        "project_id": project_id,
                        "goal": goal,
                        "state": "FAILED",
                        "admission": "CLOSED",
                        "error": str(e),
                    }
                    self.outbox.export_result(
                        cmd_id,
                        result_payload,
                        base_sha=trusted_grant.base_sha,
                        result_sha=current_head,
                        project_id=project_id,
                    )
            except Exception as close_err:
                logger.error(f"Error finalizing failed command: {close_err}")
            raise
