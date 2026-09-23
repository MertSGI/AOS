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
import sys
import time
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set
import datetime
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
if _SOURCE_ROOT.is_dir() and str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

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
    AgenticSessionIdentity,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.evidence_aggregator import EvidenceAggregator, EvidenceType
from extensions.autonomy_fabric.completion_supervisor import CompletionSupervisor, ControllerReviewDisposition
from extensions.autonomy_fabric.native_workers import redact_secrets
from aos.read_identity import build_read_identity, normalize_read_path


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
    completed_read_observations: List[Dict[str, Any]] = field(default_factory=list)
    agentic_execution_checkpoints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
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
        workspace_source_generation: Optional[str] = None,
        canonical_source_sha: Optional[str] = None,
    ):
        self.project_id = project_id
        self.workspace_path = workspace_path
        self.dag = dag
        self.router = router
        self.registry = registry
        self.evidence_aggregator = evidence_aggregator or EvidenceAggregator(registry)
        self.completion_supervisor = completion_supervisor or CompletionSupervisor(registry)
        self.checkpoint_file = checkpoint_file
        self.workspace_source_generation = workspace_source_generation or hashlib.sha256(
            f"legacy:{project_id}".encode("utf-8")
        ).hexdigest()
        self.canonical_source_sha = canonical_source_sha

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

            if not isinstance(raw_data, dict):
                raise CheckpointCorruptionError(f"Checkpoint file {self.checkpoint_file} must contain a JSON object")

            # Checkpoint V2 strictly requires complete wrapper: schema_version, project_id, coordinator_id, checksum, state
            schema_ver = raw_data.get("schema_version")
            if schema_ver != "2.0.0":
                raise CheckpointCorruptionError(
                    f"Incompatible or missing schema_version in checkpoint: expected '2.0.0', got '{schema_ver}'"
                )

            proj_id = raw_data.get("project_id")
            if not proj_id or proj_id != self.project_id:
                raise CheckpointCorruptionError(
                    f"Checkpoint project_id mismatch: expected '{self.project_id}', got '{proj_id}'"
                )

            coord_id = raw_data.get("coordinator_id")
            if not coord_id or coord_id != self.state.coordinator_id:
                raise CheckpointCorruptionError(
                    f"Checkpoint coordinator_id mismatch: expected '{self.state.coordinator_id}', got '{coord_id}'"
                )

            stored_checksum = raw_data.get("checksum")
            state_dict = raw_data.get("state")
            if not stored_checksum or not isinstance(state_dict, dict):
                raise CheckpointCorruptionError(
                    f"Checkpoint missing required integrity checksum or state payload in {self.checkpoint_file}"
                )

            computed_checksum = hashlib.sha256(json.dumps(state_dict, sort_keys=True).encode("utf-8")).hexdigest()
            if stored_checksum != computed_checksum:
                raise CheckpointCorruptionError(
                    f"Checkpoint integrity verification failed for {self.checkpoint_file}: checksum mismatch"
                )

            data = state_dict

            self.state.completed_task_ids = data.get("completed_task_ids", [])
            self.state.failed_task_ids = data.get("failed_task_ids", [])
            observations = data.get("completed_read_observations", [])
            self.state.completed_read_observations = self._validated_read_observations(observations)
            checkpoints = data.get("agentic_execution_checkpoints", {})
            self.state.agentic_execution_checkpoints = (
                dict(checkpoints) if isinstance(checkpoints, dict) else {}
            )
            self.state.iteration_count = data.get("iteration_count", 0)
            self.state.last_checkpoint = data.get("last_checkpoint", "")
            self.state.schema_version = "2.0.0"

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

    def _validated_read_observations(self, values: Any) -> List[Dict[str, Any]]:
        if not isinstance(values, list):
            return []
        validated: List[Dict[str, Any]] = []
        for value in values[-128:]:
            if not isinstance(value, dict):
                continue
            try:
                normalized_path = normalize_read_path(str(value.get("normalized_path", "")))
                content_hash = str(value.get("content_sha256", "")).lower()
                generation = str(value.get("workspace_source_generation", "")).lower()
                identity = build_read_identity(
                    normalized_path=normalized_path,
                    content_sha256=content_hash,
                    workspace_source_generation=generation,
                )
                target = os.path.abspath(os.path.join(self.workspace_path, normalized_path))
                root = os.path.abspath(self.workspace_path)
                if os.path.commonpath((root, target)) != root:
                    continue
                character_count = int(value.get("character_count", 0))
                excerpt = redact_secrets(str(value.get("redacted_excerpt", "")))[:4000]
            except (TypeError, ValueError, OSError):
                continue
            validated.append({
                "schema_version": "1.0.0",
                "read_identity": identity,
                "normalized_path": normalized_path,
                "content_sha256": content_hash,
                "workspace_source_generation": generation,
                "character_count": max(0, min(character_count, 100_000_000)),
                "redacted_excerpt": excerpt,
            })
        return validated

    def _record_completed_read(self, node: DAGNode, result: ExecutionResult) -> None:
        payload = getattr(node, "payload", {})
        if not isinstance(payload, dict) or payload.get("action") != "read_file":
            return
        raw_path = str(payload.get("path", ""))
        try:
            normalized_path = normalize_read_path(raw_path)
            target = os.path.abspath(os.path.join(self.workspace_path, raw_path))
            root = os.path.abspath(self.workspace_path)
            if os.path.commonpath((root, target)) != root:
                return
            content_hash = None
            for key, value in result.artifact_hashes.items():
                if normalize_read_path(str(key)) == normalized_path:
                    content_hash = str(value).lower()
                    break
            if content_hash is None:
                return
            read_identity = build_read_identity(
                normalized_path=normalized_path,
                content_sha256=content_hash,
                workspace_source_generation=self.workspace_source_generation,
            )
            content = str(result.evidence_payload.get("content", ""))
            observation = {
                "schema_version": "1.0.0",
                "read_identity": read_identity,
                "normalized_path": normalized_path,
                "content_sha256": content_hash,
                "workspace_source_generation": self.workspace_source_generation,
                "character_count": len(content),
                "redacted_excerpt": redact_secrets(content)[:4000],
            }
        except (TypeError, ValueError, OSError):
            return
        self.state.completed_read_observations = [
            existing for existing in self.state.completed_read_observations
            if existing.get("read_identity") != read_identity
        ][-127:] + [observation]

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
            if node.run_type in ("FILE", "PATCH", "FILE_WRITE", "DEV"):
                action = payload.get("action")
                if action == "read_file":
                    caps = [ExecutionCapability.FILE_READ]
                elif action == "write_file":
                    caps = [ExecutionCapability.FILE_WRITE]
                else:
                    caps = [ExecutionCapability.PATCH_APPLY]
                if not payload:
                    # Default empty no-op file or patch payload for test runs
                    payload = {"action": "write_file", "path": f".aos_{node.node_id}.txt", "content": "done\n"}
            elif node.run_type in ("GIT", "GIT_COMMIT", "GIT_BRANCH"):
                read_actions = {"status", "diff", "log", "show", "rev-parse", "branch", "remote", "ls-files"}
                caps = [
                    ExecutionCapability.GIT_READ
                    if str(payload.get("action", "")).lower() in read_actions
                    else ExecutionCapability.GIT_WRITE
                ]
            elif node.run_type in ("REASONING", "MODEL_REASONING", "PLAN"):
                caps = [ExecutionCapability.MODEL_REASONING]
            elif node.run_type in ("BROWSER", "VISUAL"):
                caps = [ExecutionCapability.BROWSER]
            elif node.run_type in ("CI", "CI_OBSERVE"):
                caps = [ExecutionCapability.CI_OBSERVE]
            elif node.run_type in ("TEST", "BUILD", "PROCESS"):
                caps = [ExecutionCapability.PROCESS_EXEC]
                if not payload:
                    payload = {"cmd": ["python", "-c", "pass"]}
            elif node.run_type in ("AGENTIC", "ANTIGRAVITY", "CODEX"):
                caps = [ExecutionCapability.LONG_HORIZON_AGENTIC_WORK]

            prior_identity = None
            raw_identity = self.state.agentic_execution_checkpoints.get(node.node_id)
            if isinstance(raw_identity, dict):
                try:
                    prior_identity = AgenticSessionIdentity(**raw_identity)
                except (TypeError, ValueError):
                    raise CheckpointCorruptionError(
                        f"Invalid agentic identity for completed node {node.node_id}"
                    )

            req = ExecutionRequest(
                task_id=node.node_id,
                project_id=self.project_id,
                workspace=self.workspace_path,
                operation_class=node.run_type,
                required_capabilities=caps,
                authority_id=node.authority_id,
                write_scope=list(node.write_scope),
                expected_artifacts=list(node.expected_artifacts),
                payload={
                    **payload,
                    **(
                        {"source_sha": self.canonical_source_sha}
                        if self.canonical_source_sha and "source_sha" not in payload
                        else {}
                    ),
                },
                agentic_identity=prior_identity,
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
                if res.agentic_identity is not None:
                    identity = res.agentic_identity
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
                    self.state.agentic_execution_checkpoints[node.node_id] = identity.to_dict()
                self.registry.transition(run.run_id, RunStatus.COMPLETED, phase="EXECUTION_SUCCESS")
                if node.node_id not in self.state.completed_task_ids:
                    self.state.completed_task_ids.append(node.node_id)
                self._record_completed_read(node, res)
                if node.gate_type != NodeGateType.NONE:
                    self.dag.pass_gate(node.node_id)
            elif res.status in ("DEGRADED", "WAITING"):
                self.registry.transition(
                    run.run_id,
                    RunStatus.WAITING_AGENT,
                    phase="AGENTIC_RESOURCE_REROUTE",
                    payload={"errors": res.sanitized_errors},
                )
            else:
                self.registry.transition(
                    run.run_id, RunStatus.FAILED, phase="EXECUTION_FAILURE", payload={"errors": res.sanitized_errors}
                )
                if node.node_id not in self.state.failed_task_ids:
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

            results = self.execute_next_batch(max_tasks=3)
            if results and not any(result.status == "SUCCESS" for result in results):
                # A failed node remains eligible in the DAG so it can be retried
                # after replanning, but spinning on it in the same bounded batch
                # only duplicates work and evidence.
                break

        self._save_checkpoint()
        return self.state
