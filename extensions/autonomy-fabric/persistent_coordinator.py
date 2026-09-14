"""AOS Persistent Coordinator & Checkpoint Runner (V2).

Implements bounded persistent coordination loop:
- Loads project state and leases from durable journals.
- Selects eligible ready tasks from TaskDAG.
- Dispatches execution requests across native workers without requiring Antigravity or chat wakeups.
- Observes results and evidence, evaluates gates, records progress, and persists checkpoints.
- Recovers state cleanly across process restarts.
"""

from __future__ import annotations

import os
import time
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set
import datetime

from extensions.autonomy_fabric.run_registry import (
    AgentRunRegistry,
    RunStatus,
    RunIdentity,
    FileRunJournal,
)
from extensions.autonomy_fabric.task_dag import TaskDAG, DAGNode, NodeGateType
from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    EvidenceClass,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.evidence_aggregator import EvidenceAggregator, EvidenceType
from extensions.autonomy_fabric.completion_supervisor import CompletionSupervisor, ControllerReviewDisposition


class CheckpointCorruptionError(ValueError):
    """Raised when checkpoint is corrupt, truncated, or incompatible."""
    pass


@dataclass
class CoordinatorState:
    coordinator_id: str
    project_id: str
    running: bool = False
    completed_task_ids: List[str] = field(default_factory=list)
    failed_task_ids: List[str] = field(default_factory=list)
    iteration_count: int = 0
    schema_version: str = "2.0.0"
    last_checkpoint: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PersistentCoordinator:
    """Long-running, restartable coordinator process loop."""

    def __init__(
        self,
        project_id: str,
        workspace_path: str,
        dag: TaskDAG,
        router: ExecutionRouter,
        registry: AgentRunRegistry,
        evidence_aggregator: Optional[EvidenceAggregator] = None,
        completion_supervisor: Optional[CompletionSupervisor] = None,
        checkpoint_file: Optional[str] = None,
    ):
        self.project_id = project_id
        self.workspace_path = workspace_path
        self.dag = dag
        self.router = router
        self.registry = registry
        self.evidence_aggregator = evidence_aggregator or EvidenceAggregator(registry)
        self.completion_supervisor = completion_supervisor or CompletionSupervisor(registry)
        self.checkpoint_file = checkpoint_file

        self.state = CoordinatorState(
            coordinator_id=f"coord-{project_id}",
            project_id=project_id,
        )
        self._load_checkpoint()

    def _load_checkpoint(self):
        if self.checkpoint_file and os.path.exists(self.checkpoint_file):
            try:
                with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
            except Exception as ex:
                # FAIL CLOSED on corrupt or truncated JSON
                raise CheckpointCorruptionError(
                    f"Checkpoint file {self.checkpoint_file} is corrupt or truncated: {ex}"
                )

            # Integrity verification
            stored_checksum = raw_data.get("checksum")
            state_dict = raw_data.get("state")
            if stored_checksum and state_dict:
                computed_checksum = hashlib.sha256(json.dumps(state_dict, sort_keys=True).encode("utf-8")).hexdigest()
                if stored_checksum != computed_checksum:
                    raise CheckpointCorruptionError(
                        f"Checkpoint integrity verification failed for {self.checkpoint_file}"
                    )
                data = state_dict
            else:
                data = raw_data

            # Validate identity binding
            if data.get("project_id") and data.get("project_id") != self.project_id:
                raise CheckpointCorruptionError(
                    f"Checkpoint project_id mismatch: expected {self.project_id}, got {data.get('project_id')}"
                )

            self.state.completed_task_ids = data.get("completed_task_ids", [])
            self.state.failed_task_ids = data.get("failed_task_ids", [])
            self.state.iteration_count = data.get("iteration_count", 0)
            self.state.last_checkpoint = data.get("last_checkpoint", "")
            self.state.schema_version = data.get("schema_version", "2.0.0")

            # Rehydrate completed nodes into DAG & Registry across process restarts
            for completed_id in self.state.completed_task_ids:
                if completed_id in self.dag.nodes:
                    node = self.dag.nodes[completed_id]
                    synth_run_id = f"recovered-run-{completed_id}"
                    if not self.registry.get_run(synth_run_id):
                        run = self.registry.create_run(
                            project_id=self.project_id,
                            run_type=node.run_type,
                            authority_id=node.authority_id,
                            controller_id=self.state.coordinator_id,
                            agent_provider="recovered_checkpoint",
                            workspace_path=self.workspace_path,
                            run_id=synth_run_id,
                        )
                        self.registry.transition(run.run_id, RunStatus.STARTING)
                        self.registry.transition(run.run_id, RunStatus.RUNNING)
                        self.registry.transition(run.run_id, RunStatus.COMPLETED)
                    self.dag.associate_run(completed_id, synth_run_id)

    def _save_checkpoint(self):
        if self.checkpoint_file:
            self.state.last_checkpoint = datetime.datetime.now(datetime.timezone.utc).isoformat()
            state_dict = self.state.to_dict()
            checksum = hashlib.sha256(json.dumps(state_dict, sort_keys=True).encode("utf-8")).hexdigest()
            payload = {
                "schema_version": self.state.schema_version,
                "project_id": self.project_id,
                "coordinator_id": self.state.coordinator_id,
                "checksum": checksum,
                "state": state_dict,
            }
            tmp_path = f"{self.checkpoint_file}.tmp"
            os.makedirs(os.path.dirname(os.path.abspath(self.checkpoint_file)), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.checkpoint_file)

    def execute_next_batch(self, max_tasks: int = 5) -> List[ExecutionResult]:
        """Runs one bounded batch of eligible unblocked DAG tasks."""
        results: List[ExecutionResult] = []
        eligible_nodes = self.dag.get_eligible_nodes()

        for node in eligible_nodes[:max_tasks]:
            if node.node_id in self.state.completed_task_ids:
                continue

            self.state.iteration_count += 1

            # 1. Create run in registry
            run = self.registry.create_run(
                project_id=self.project_id,
                run_type=node.run_type,
                authority_id=node.authority_id,
                controller_id=self.state.coordinator_id,
                agent_provider="native_router",
                workspace_path=self.workspace_path,
            )
            self.dag.associate_run(node.node_id, run.run_id)
            self.registry.transition(run.run_id, RunStatus.STARTING, phase="DISPATCH")

            # 2. Build execution request
            caps = [ExecutionCapability.PROCESS_EXEC]
            payload = getattr(node, "payload", {})
            if node.run_type in ("PATCH", "FILE_WRITE", "DEV"):
                caps = [ExecutionCapability.FILE_WRITE] if payload.get("action") == "write_file" else [ExecutionCapability.PATCH_APPLY]
                if not payload:
                    # Default empty no-op file or patch payload for test runs
                    payload = {"action": "write_file", "path": f".aos_{node.node_id}.txt", "content": "done\n"}
            elif node.run_type in ("GIT_COMMIT", "GIT_BRANCH"):
                caps = [ExecutionCapability.GIT_WRITE]
            elif node.run_type in ("REASONING", "MODEL_REASONING", "PLAN"):
                caps = [ExecutionCapability.MODEL_REASONING]
            elif node.run_type in ("TEST", "BUILD", "PROCESS"):
                caps = [ExecutionCapability.PROCESS_EXEC]
                if not payload:
                    payload = {"cmd": ["python", "-c", "pass"]}

            req = ExecutionRequest(
                task_id=node.node_id,
                project_id=self.project_id,
                workspace=self.workspace_path,
                operation_class=node.run_type,
                required_capabilities=caps,
                authority_id=node.authority_id,
                payload=payload,
            )

            # 3. Route & execute with failover
            self.registry.transition(run.run_id, RunStatus.RUNNING, phase="NATIVE_EXECUTION")
            res = self.router.execute_with_failover(req)
            results.append(res)

            # 4. Record evidence
            ev_type = EvidenceType.VERIFICATION if res.status == "SUCCESS" else EvidenceType.OBSERVATION
            self.evidence_aggregator.record_evidence(
                run_id=run.run_id,
                phase=node.run_type,
                project_id=self.project_id,
                evidence_type=ev_type,
                producer=f"coordinator:{res.backend_id}",
                title=f"Execution of task {node.node_id}",
                payload=res.to_dict(),
            )

            # 5. Update status
            if res.status == "SUCCESS":
                self.registry.transition(run.run_id, RunStatus.COMPLETED, phase="EXECUTION_SUCCESS")
                self.state.completed_task_ids.append(node.node_id)
                if node.gate_type != NodeGateType.NONE:
                    self.dag.pass_gate(node.node_id)
            else:
                self.registry.transition(
                    run.run_id, RunStatus.FAILED, phase="EXECUTION_FAILURE", payload={"errors": res.sanitized_errors}
                )
                self.state.failed_task_ids.append(node.node_id)

            self._save_checkpoint()

        return results

    def run_until_complete(self, max_iterations: int = 20) -> CoordinatorState:
        """Continuously runs batches until DAG completion or blocker."""
        iter_num = 0
        while iter_num < max_iterations:
            iter_num += 1
            progress = self.dag.compute_progress()
            if progress >= 100.0:
                break

            eligible = self.dag.get_eligible_nodes()
            uncompleted_eligible = [n for n in eligible if n.node_id not in self.state.completed_task_ids]
            if not uncompleted_eligible:
                # No ready tasks (blocked on dependency, failure, or human gate)
                break

            self.execute_next_batch(max_tasks=3)

        self._save_checkpoint()
        return self.state
