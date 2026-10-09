"""Detached command worker for AOS Runtime V1.

The worker never treats stdout as the orchestration contract. Command state,
results, and events are durable structured JSON/JSONL artifacts under the
Runtime V1 root.  On restart the planning kernel resumes from its own durable
checkpoint before new reasoning is invoked.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Set, Tuple

_HEX_SHA_40 = re.compile(r"^[0-9a-f]{40}$")

from aos.planning_kernel import (
    DEFAULT_RED_LINES,
    AuthorityDenied,
    CanonicalDrift,
    HumanRequired,
    BackendLocalReasoningFailure,
    run_autonomous_project,
)
from extensions.autonomy_fabric.native_workers import redact_secrets
from aos.runtime_contract import (
    ContinueProjectCommand,
    RuntimeResult,
    cumulative_completed_batch_count,
    utc_now,
)
from aos.runtime_store import RuntimeStore, exclusive_file_lock, read_json
from aos.secure_store import hydrate_environment
from aos.canonical_reconciler import reconcile_missing_execution_base
from aos.provider_circuit import ProviderCircuitBreakerRegistry
from aos.runtime_maintenance import is_paused
from aos.runtime_admission import CommandAdmissionStore
from aos.provider_observation import canonical_failure_family
from aos.read_identity import build_workspace_source_generation
from aos.runtime_assets import resolve_active_runtime_artifact
from aos.knowledge.hooks import resolve_knowledge_ledger
from aos.knowledge.receipts import record_verification_receipt


def recovery_failure_family(
    state: Dict[str, Any], checkpoint: Dict[str, Any]
) -> str:
    raw = (
        state.get("failure_class")
        or checkpoint.get("failure_class")
        or checkpoint.get("disposition")
        or checkpoint.get("phase")
        or checkpoint.get("reason")
        or "UNKNOWN"
    )
    text = str(raw).upper()
    if "VALIDATION" in text or "BOUNDED_RUN_EXHAUSTED" in text:
        return "PLANNER_VALIDATION"
    return canonical_failure_family(text)


def build_recovery_fingerprint(
    *,
    project_id: str,
    state: Dict[str, Any],
    checkpoint: Dict[str, Any],
) -> Dict[str, Any]:
    batch_number = int(checkpoint.get("batch_number", 0) or 0)
    completed = max(
        int(state.get("completed_batch_count", 0) or 0),
        cumulative_completed_batch_count(checkpoint),
    )
    source_sha = str(
        checkpoint.get("canonical_source_sha")
        or state.get("canonical_source_sha")
        or "UNKNOWN"
    )
    execution_base = (
        checkpoint.get("canonical_execution_base_sha")
        or state.get("canonical_execution_base_sha")
    )
    generation = build_workspace_source_generation(
        project_id=project_id,
        canonical_source_sha=source_sha,
        canonical_execution_base_sha=(str(execution_base) if execution_base else None),
    )
    fields = {
        "batch_number": batch_number,
        "completed_batch_count_baseline": completed,
        "failure_family": recovery_failure_family(state, checkpoint),
        "objective_id": checkpoint.get("objective_id") or None,
        "workspace_source_generation": generation,
        "candidate_sha": state.get("candidate_sha") or checkpoint.get("candidate_sha") or None,
        "delivery_stage": state.get("delivery_stage") or checkpoint.get("delivery_stage") or None,
    }
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**fields, "fingerprint_sha256": hashlib.sha256(encoded).hexdigest()}


class PlanningArtifactWatcher(threading.Thread):
    def __init__(self, store: RuntimeStore, command_id: str, project_runtime: Path, stop: threading.Event) -> None:
        super().__init__(name=f"aos-artifact-watcher-{command_id}", daemon=True)
        self.store = store
        self.command_id = command_id
        self.project_runtime = project_runtime
        self.stop_event = stop
        self.seen: Set[str] = set()
        self.last_checkpoint_signature: Optional[str] = None
        self._prime_existing_artifacts()

    def _prime_existing_artifacts(self) -> None:
        """Baseline durable artifacts so a recovered worker emits only changes."""
        if not self.project_runtime.is_dir():
            return
        for pattern in ("situation-*.json", "objective-*.json", "batches/batch-*/generated-run-plan.json"):
            for path in self.project_runtime.glob(pattern):
                self.seen.add(path.relative_to(self.project_runtime).as_posix())
        checkpoint = read_json(self.project_runtime / "planning-kernel-checkpoint.json")
        if checkpoint:
            self.last_checkpoint_signature = self._checkpoint_signature(checkpoint)

    @staticmethod
    def _checkpoint_signature(data: Dict[str, Any]) -> str:
        return json.dumps({
            "phase": data.get("phase"),
            "batch_number": data.get("batch_number"),
            "completed_batch_count": cumulative_completed_batch_count(data),
            "replan_reason": data.get("replan_reason"),
            "canonical_source_sha": data.get("canonical_source_sha"),
        }, sort_keys=True)

    def _emit_file(self, path: Path) -> None:
        rel = path.relative_to(self.project_runtime).as_posix()
        if rel in self.seen:
            return
        data = read_json(path)
        if not data:
            return
        self.seen.add(rel)
        if path.name.startswith("situation-"):
            self.store.append_event(self.command_id, "project.situation", {
                "artifact": rel,
                "project_id": data.get("project_id"),
                "control_sha": data.get("control_sha") or data.get("canonical_source_sha"),
                "execution_base_sha": data.get("execution_base_sha") or data.get("canonical_execution_base_sha"),
                "ambiguity_reason_count": len(data.get("ambiguity_reasons", []) or []),
            })
        elif path.name.startswith("objective-"):
            self.store.append_event(self.command_id, "objective.selected", {
                "artifact": rel,
                "objective_id": data.get("objective_id"),
                "title": data.get("title"),
                "risk_class": data.get("risk_class"),
                "authority_id": data.get("authority_id"),
            })
        elif path.name == "generated-run-plan.json":
            tasks = data.get("tasks", []) if isinstance(data.get("tasks"), list) else []
            groups = data.get("parallel_safe_groups", []) if isinstance(data.get("parallel_safe_groups"), list) else []
            self.store.append_event(self.command_id, "dag.generated", {
                "artifact": rel,
                "objective_id": data.get("objective_id"),
                "task_count": len(tasks),
                "parallel_group_count": len(groups),
            })

    def _emit_checkpoint(self) -> None:
        path = self.project_runtime / "planning-kernel-checkpoint.json"
        data = read_json(path)
        if not data:
            return
        signature = self._checkpoint_signature(data)
        if signature == self.last_checkpoint_signature:
            return
        self.last_checkpoint_signature = signature
        phase = str(data.get("phase") or "UNKNOWN")
        event_type = {
            "EXECUTING": "batch.executing",
            "BATCH_COMPLETE": "batch.completed",
            "PROJECT_COMPLETE": "run.project_complete",
            "HUMAN_REQUIRED": "run.human_required",
            "WAITING_FOR_REASONING_PROVIDER": "run.waiting_for_reasoning_provider",
            "BOUNDED_RUN_EXHAUSTED": "run.replan_boundary",
        }.get(phase, "checkpoint.updated")
        self.store.append_event(self.command_id, event_type, {
            "phase": phase,
            "batch_number": data.get("batch_number"),
            "completed_batch_count": cumulative_completed_batch_count(data),
            "replan_reason": data.get("replan_reason"),
            "canonical_source_sha": data.get("canonical_source_sha"),
            "canonical_execution_base_sha": data.get("canonical_execution_base_sha"),
        })

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.project_runtime.is_dir():
                    for pattern in ("situation-*.json", "objective-*.json"):
                        for path in sorted(self.project_runtime.glob(pattern)):
                            self._emit_file(path)
                    for path in sorted(self.project_runtime.glob("batches/batch-*/generated-run-plan.json")):
                        self._emit_file(path)
                    self._emit_checkpoint()
            except Exception:
                # Watcher telemetry must never crash project execution.
                pass
            self.stop_event.wait(0.5)


def _active_runtime_artifact_path(
    stored_path: str,
) -> Path:
    """Rebind AOS-owned descriptor/policy files to the executing candidate slot.

    Command lineage, product workspace, goal and history remain unchanged.
    Only immutable AOS runtime-owned configuration follows the exact candidate
    that launched this worker.
    """
    return resolve_active_runtime_artifact(stored_path)


_SOURCE_TRANSPORT_CONTEXT_MARKERS = (
    "failed to resolve revision",
    "failed to resolve ref",
    "failed to fetch file",
    "github actions api",
)

_SOURCE_TRANSPORT_TRANSIENT_MARKERS = (
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "remote end closed connection",
    "connection reset",
    "timed out",
    "timeout",
    "urlopen error",
)


def _is_transient_source_transport_error(
    exc: BaseException,
) -> bool:
    message = _safe_exception_message(
        exc
    ).lower()

    return (
        any(
            marker in message
            for marker in _SOURCE_TRANSPORT_CONTEXT_MARKERS
        )
        and
        any(
            marker in message
            for marker in _SOURCE_TRANSPORT_TRANSIENT_MARKERS
        )
    )


def _pause_safe_cycle_result(
    store: RuntimeStore,
    command_id: str,
    project_runtime: Path,
    *,
    cycle: int,
    receipt: Dict[str, Any],
) -> Dict[str, Any]:
    """Stop a continuous worker at a durable cycle boundary.

    PAUSED_SAFE allows an already in-flight planning cycle to finish, but it
    must not silently begin another continuous cycle after maintenance has
    been persisted.  Keep the lineage non-terminal so explicit resume can
    recover it from the durable checkpoint without creating a new command.
    """
    checkpoint = (
        read_json(
            project_runtime
            / "planning-kernel-checkpoint.json"
        )
        or {}
    )

    completed = (
        cumulative_completed_batch_count(
            checkpoint,
            receipt,
        )
    )

    canonical_source_sha = (
        checkpoint.get(
            "canonical_source_sha"
        )
        or
        receipt.get(
            "canonical_source_sha"
        )
    )

    canonical_execution_base_sha = (
        checkpoint.get(
            "canonical_execution_base_sha"
        )
        or
        receipt.get(
            "canonical_execution_base_sha"
        )
    )

    paused_receipt = dict(
        receipt
        or {}
    )

    paused_receipt[
        "pause_safe"
    ] = True

    paused_receipt[
        "pause_boundary_cycle"
    ] = int(cycle)

    result = RuntimeResult(
        command_id=command_id,
        state="RUNNING",
        disposition="PAUSED_SAFE",
        completed_batch_count=completed,
        canonical_source_sha=canonical_source_sha,
        canonical_execution_base_sha=canonical_execution_base_sha,
        receipt=paused_receipt,
    ).to_dict()

    store.write_result(
        command_id,
        result,
    )

    store.write_state(
        command_id,
        state="RUNNING",
        disposition="PAUSED_SAFE",
        completed_batch_count=completed,
        canonical_source_sha=canonical_source_sha,
        canonical_execution_base_sha=canonical_execution_base_sha,
        worker_pid=None,
        retry_after_epoch=0,
    )

    store.append_event(
        command_id,
        "runtime.worker_paused_safe",
        {
            "cycle":
                int(cycle),

            "completed_batch_count":
                completed,

            "lineage_preserved":
                True,

            "checkpoint_preserved":
                True,
        },
    )

    return result


def _admission_hold_cycle_result(
    store: RuntimeStore,
    command_id: str,
    *,
    cycle: int,
) -> Dict[str, Any]:
    """Stop at a durable cycle boundary when command admission is not ACTIVE."""
    admission = CommandAdmissionStore(store.runtime_root).get(command_id)
    state = store.read_state(command_id)
    result = RuntimeResult(
        command_id=command_id,
        state="TECHNICAL_HOLD",
        disposition="COMMAND_ADMISSION_HOLD",
        completed_batch_count=int(state.get("completed_batch_count", 0) or 0),
        canonical_source_sha=state.get("canonical_source_sha"),
        canonical_execution_base_sha=state.get("canonical_execution_base_sha"),
        receipt={
            "reason": admission.reason,
            "admission_state": admission.state,
            "legacy_missing_record": admission.legacy_missing_record,
            "cycle_boundary": int(cycle),
        },
    ).to_dict()
    store.write_result(command_id, result)
    store.write_state(
        command_id,
        state="TECHNICAL_HOLD",
        disposition="COMMAND_ADMISSION_HOLD",
        worker_pid=None,
    )
    store.append_event(command_id, "runtime.worker_admission_held", {
        "cycle": int(cycle),
        "admission_state": admission.state,
        "reason": admission.reason,
        "lineage_preserved": True,
    })
    return result


def _safe_exception_message(exc: BaseException) -> str:
    return redact_secrets(str(exc))[:1500]


def _classify_exception(exc: BaseException) -> tuple[str, str]:
    message = _safe_exception_message(exc).lower()
    if isinstance(exc, BackendLocalReasoningFailure):
        return "TECHNICAL_HOLD", "BACKEND_LOCAL_REASONING_FAILURE"
    if isinstance(exc, (HumanRequired, AuthorityDenied, CanonicalDrift)):
        return "HUMAN_REQUIRED", exc.__class__.__name__.upper()
    canonical_markers = (
        "missing required execution base sha",
        "next_action_execution_base_sha",
        "target base sha",
        "canonical contradiction",
        "canonical drift",
    )
    if any(marker in message for marker in canonical_markers):
        return "HUMAN_REQUIRED", "CANONICAL_CONTRADICTION"
    if isinstance(exc, FileNotFoundError):
        return "FAILED", "READONLY_OR_RUNTIME_EXECUTABLE_MISSING"
    return "FAILED", "RUNTIME_EXECUTION_FAILURE"


def _terminal_state(disposition: str) -> str:
    if disposition == "PROJECT_COMPLETE":
        return "PROJECT_COMPLETE"
    if disposition == "HUMAN_REQUIRED":
        return "HUMAN_REQUIRED"
    if disposition == "WAITING_FOR_REASONING_PROVIDER":
        return "WAITING_FOR_REASONING_PROVIDER"
    if disposition == "BOUNDED_RUN_EXHAUSTED":
        return "RUNNING"
    if disposition == "TECHNICAL_HOLD":
        return "TECHNICAL_HOLD"
    if disposition == "WAITING_FOR_CI":
        return "WAITING_FOR_CI"
    if disposition == "DELIVERING":
        return "DELIVERING"
    return "FAILED"


def _workspace_lock_path(runtime_root: Path, workspace_dir: Path) -> Path:
    """Return a machine-local lock path without mutating product workspaces.

    Some accepted product lineages track the historical root lock filename.
    Holding that tracked file open on Windows makes its content unreadable to
    the content-sensitive workspace fingerprint.  Key the coordination lock by
    the canonical workspace path under Runtime V1 state instead, preserving
    cross-command mutual exclusion without hiding or changing tracked source.
    """
    normalized = os.path.normcase(str(workspace_dir.expanduser().resolve()))
    identity = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    return runtime_root.expanduser().resolve() / "workspace-locks" / f"{identity}.lock"


def _is_sha40(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    return bool(_HEX_SHA_40.match(val.strip().lower()))


def _extract_completed_batch_receipts(
    receipt: Mapping[str, Any],
    project_runtime: Optional[Path] = None,
) -> Tuple[list[Mapping[str, Any]], set[str], set[str], bool]:
    """Extract and validate completed batch execution evidence from receipt and project-runtime disk.

    Returns:
        (valid_batches, cumulative_completed_task_ids, cumulative_failed_task_ids, any_batch_failed)
    """
    raw_batches = (
        receipt.get("completed_batches")
        or receipt.get("recent_completed_batches")
        or []
    )
    all_completed_tasks: set[str] = set()
    all_failed_tasks: set[str] = set()
    valid_batches: list[Mapping[str, Any]] = []
    has_failed = False

    # Also capture top-level task IDs if present
    for tid in receipt.get("completed_task_ids", []):
        s_tid = str(tid).strip()
        if s_tid:
            all_completed_tasks.add(s_tid)
    for tid in receipt.get("failed_task_ids", []):
        s_tid = str(tid).strip()
        if s_tid:
            all_failed_tasks.add(s_tid)
            has_failed = True

    if isinstance(raw_batches, list):
        for b_entry in raw_batches:
            if not isinstance(b_entry, Mapping):
                continue
            b_num = b_entry.get("batch_number")
            nested_receipt = b_entry.get("receipt") if isinstance(b_entry.get("receipt"), Mapping) else b_entry

            # If project_runtime is available, validate against disk artifacts in batches/batch-XXXX/
            if project_runtime is not None and b_num is not None:
                try:
                    b_int = int(b_num)
                    disk_batch_dir = project_runtime / "batches" / f"batch-{b_int:04d}"
                    if not disk_batch_dir.is_dir():
                        disk_batch_dir = project_runtime / "batches" / f"batch-{b_int}"
                    if disk_batch_dir.is_dir():
                        host_receipt_file = disk_batch_dir / "host-receipt.json"
                        if host_receipt_file.is_file():
                            disk_receipt = read_json(host_receipt_file)
                            if isinstance(disk_receipt, Mapping):
                                nested_receipt = disk_receipt
                except (ValueError, TypeError):
                    pass

            b_completed = [
                str(t).strip()
                for t in nested_receipt.get("completed_task_ids", [])
                if str(t).strip()
            ]
            b_failed = [
                str(t).strip()
                for t in nested_receipt.get("failed_task_ids", [])
                if str(t).strip()
            ]
            raw_prog = nested_receipt.get("progress")
            try:
                prog = float(raw_prog) if raw_prog is not None else 100.0
            except (ValueError, TypeError):
                prog = 0.0

            if b_failed or prog < 100.0:
                has_failed = True
                for f_id in b_failed:
                    all_failed_tasks.add(f_id)
            elif b_completed:
                valid_batches.append(nested_receipt)
                for c_id in b_completed:
                    all_completed_tasks.add(c_id)

    # Check top-level progress if present
    top_prog = receipt.get("progress")
    if top_prog is not None:
        try:
            if float(top_prog) < 100.0:
                has_failed = True
        except (ValueError, TypeError):
            pass

    return valid_batches, all_completed_tasks, all_failed_tasks, has_failed


def _record_kcp_verification_receipt(
    runtime_root: Path,
    *,
    project_id: str,
    command_id: str,
    receipt: Mapping[str, Any],
    project_runtime: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Record a KCP verification receipt for verified completed worker outcomes.

    The authoritative KnowledgeLedger is resolved exclusively from the
    Runtime V1 root without depending on cwd or ambient environment overrides.
    A valid verification receipt requires real execution evidence: completed
    tasks and a confirmed execution SHA.
    """
    if not receipt:
        return None

    disposition = str(receipt.get("disposition") or "").upper()
    if disposition in ("FAILED", "HUMAN_REQUIRED", "TECHNICAL_HOLD"):
        return None

    # Never treat a generated DAG alone as execution evidence
    if receipt.get("run_plan_only") or receipt.get("generated_dag_only"):
        return None

    valid_batches, completed_task_ids, failed_task_ids, any_batch_failed = (
        _extract_completed_batch_receipts(receipt, project_runtime=project_runtime)
    )

    if any_batch_failed or failed_task_ids:
        return None

    if not completed_task_ids and not valid_batches:
        return None

    # EXACT SHA IDENTITY:
    # 1. Control SHA represents canonical control revision, NOT verified product code.
    #    Preserve it separately as lineage information.
    # 2. For read-only baseline verification, bind result_sha to verified canonical_execution_base_sha.
    # 3. For actual candidate verification, use candidate_sha only if that exact candidate is supported by genuine verification evidence.
    # 4. Never substitute control SHA for product SHA.
    # 5. Validate exact 40-character lowercase hexadecimal SHAs.
    # 6. If code identity or verification target is ambiguous, do not emit a successful KCP verification receipt.

    candidate_sha_raw = receipt.get("candidate_sha")
    candidate_sha = str(candidate_sha_raw).strip().lower() if candidate_sha_raw else ""

    control_sha_raw = receipt.get("canonical_source_sha") or receipt.get("control_sha")
    control_sha = str(control_sha_raw).strip().lower() if control_sha_raw else ""

    exec_base_sha_raw = (
        receipt.get("canonical_execution_base_sha")
        or receipt.get("base_sha")
        or receipt.get("execution_base_sha")
    )
    exec_base_sha = str(exec_base_sha_raw).strip().lower() if exec_base_sha_raw else ""

    # Determine product result SHA
    result_sha: Optional[str] = None
    base_sha_val: Optional[str] = exec_base_sha if _is_sha40(exec_base_sha) else None

    # Determine whether this is a read-only baseline verification or actual candidate verification
    is_read_only = bool(
        receipt.get("read_only_verification")
        or (not candidate_sha and _is_sha40(exec_base_sha))
    )

    if candidate_sha:
        if not _is_sha40(candidate_sha):
            # Ambiguous / invalid candidate SHA
            return None
        # Candidate SHA must be supported by genuine verification evidence
        result_sha = candidate_sha
    elif is_read_only and _is_sha40(exec_base_sha):
        # For read-only baseline verification, bind result_sha to verified canonical_execution_base_sha
        result_sha = exec_base_sha
    elif _is_sha40(exec_base_sha) and not control_sha:
        result_sha = exec_base_sha
    else:
        # Never substitute control SHA for product SHA.
        # If code identity or verification target is ambiguous, do not emit receipt.
        return None

    if not result_sha or not _is_sha40(result_sha):
        return None

    ledger = resolve_knowledge_ledger(
        runtime_root=runtime_root,
        allow_environment_override=False,
    )
    if ledger is None:
        raise RuntimeError("KCP_LEDGER_UNAVAILABLE")

    batch_number = receipt.get("batch_number", 0)
    idempotency_key = (
        f"runtime-worker-verification:{project_id}:{command_id}:batch-{batch_number}:{result_sha}"
    )

    evidence_refs = []
    if candidate_sha and _is_sha40(candidate_sha):
        evidence_refs.append(f"candidate:{candidate_sha}")
    if control_sha and _is_sha40(control_sha):
        evidence_refs.append(f"control_lineage:{control_sha}")
    if exec_base_sha and _is_sha40(exec_base_sha):
        evidence_refs.append(f"execution_base:{exec_base_sha}")
    if receipt.get("design_intelligence_execution_id"):
        evidence_refs.append(f"di:{receipt['design_intelligence_execution_id']}")
    evidence_refs.append(f"command:{command_id}")

    verification_payload = {
        "status": "PASS",
        "completed_task_count": len(completed_task_ids),
        "disposition": disposition or "VERIFIED",
    }
    if receipt.get("ci_workflow_identity"):
        verification_payload["ci_workflow"] = receipt["ci_workflow_identity"]

    claims_payload = {
        "command_id": command_id,
        "batch_number": int(batch_number or 0),
        "verified_worker_execution": True,
    }
    if control_sha and _is_sha40(control_sha):
        claims_payload["canonical_control_sha"] = control_sha
    if is_read_only:
        claims_payload["read_only_verification"] = True

    return record_verification_receipt(
        ledger,
        project_id=project_id,
        idempotency_key=idempotency_key,
        agent_class="AOS_NATIVE",
        tool_name="aos.runtime_worker",
        result_sha=result_sha,
        base_sha=base_sha_val,
        module_ids=[project_id],
        verification=verification_payload,
        evidence_refs=evidence_refs,
        claims=claims_payload,
    )



def execute_command(runtime_root: Path, command_id: str) -> Dict[str, Any]:
    store = RuntimeStore(runtime_root)
    raw = store.read_command(command_id)
    if not raw:
        raise ValueError(f"Command not found: {command_id}")
    if not CommandAdmissionStore(runtime_root).is_active(command_id):
        return _admission_hold_cycle_result(store, command_id, cycle=0)
    command = ContinueProjectCommand.from_mapping(raw)
    command_root = store.command_dir(command_id)
    project_runtime = command_root / "project-runtime"
    project_runtime.mkdir(parents=True, exist_ok=True)

    worker_lock = command_root / "worker.lock"
    workspace_dir = Path(command.project.workspace).expanduser().resolve()
    workspace_lock = _workspace_lock_path(runtime_root, workspace_dir)
    workspace_lock.parent.mkdir(parents=True, exist_ok=True)
    ws_lock_ctx = None
    with exclusive_file_lock(worker_lock):
        try:
            ws_lock_ctx = exclusive_file_lock(workspace_lock, blocking=False)
            ws_lock_handle = ws_lock_ctx.__enter__()
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Workspace conflict: workspace '{workspace_dir}' is already actively locked by another running command or worker"
            ) from exc

        try:
            previous = store.read_state(command_id)
            initial_attempts = int(previous.get("attempts", 0))
            attempts = initial_attempts + 1
            strategy_generation = int(previous.get("strategy_generation", 0) or 0)
            recovery_failure_context = (
                dict(previous.get("recovery_fingerprint", {}))
                if isinstance(previous.get("recovery_fingerprint"), dict) else {}
            )
            recovered = str(previous.get("state")) in ("RUNNING", "RECOVERING") or attempts > 1
            store.write_state(
                command_id,
                state="RUNNING",
                worker_pid=os.getpid(),
                attempts=attempts,
                recovery_count=int(previous.get("recovery_count", 0)) + (1 if recovered else 0),
            )
            store.append_event(command_id, "runtime.worker_started", {
                "worker_pid": os.getpid(),
                "attempt": attempts,
                "recovered": recovered,
            })
            if recovered:
                checkpoint = read_json(project_runtime / "planning-kernel-checkpoint.json") or {}
                store.append_event(command_id, "runtime.worker_recovered", {
                    "checkpoint_phase": checkpoint.get("phase"),
                    "batch_number": checkpoint.get("batch_number"),
                    "completed_batch_count": cumulative_completed_batch_count(checkpoint),
                })

            stop = threading.Event()
            watcher: Optional[PlanningArtifactWatcher] = None
            receipt: Dict[str, Any] = {}
            cycle = 0
            prior_repair = read_json(project_runtime / "canonical-repair.json")
            canonical_repair_attempted = bool(prior_repair)
            hydrate_environment(overwrite=True)

            active_descriptor_path = (
                _active_runtime_artifact_path(
                    command.project.descriptor_path
                )
            )

            active_routing_policy_path = (
                _active_runtime_artifact_path(
                    command.project.routing_policy_path
                )
            )

            watcher = PlanningArtifactWatcher(store, command_id, project_runtime, stop)
            watcher.start()
            while True:
                if not CommandAdmissionStore(runtime_root).is_active(command_id):
                    return _admission_hold_cycle_result(store, command_id, cycle=cycle)
                if is_paused(runtime_root):
                    return _pause_safe_cycle_result(
                        store,
                        command_id,
                        project_runtime,
                        cycle=cycle,
                        receipt=receipt,
                    )

                cycle += 1
                store.append_event(command_id, "continuation.cycle_started", {
                    "cycle": cycle,
                    "max_batches_per_cycle": command.max_batches_per_cycle,
                })
                receipt = run_autonomous_project(
                    descriptor_path=active_descriptor_path,
                    workspace=Path(command.project.workspace),
                    runtime_dir=project_runtime,
                    routing_policy_path=active_routing_policy_path,
                    goal=command.goal,
                    constraints=command.constraints,
                    red_lines=command.red_lines or tuple(DEFAULT_RED_LINES),
                    max_batches=command.max_batches_per_cycle,
                    max_iterations_per_batch=command.max_iterations_per_batch,
                    strategy_generation=strategy_generation,
                    recovery_failure_context=recovery_failure_context,
                )
                disposition = str(receipt.get("disposition", ""))
                completed = cumulative_completed_batch_count(receipt)

                if (
                    disposition == "HUMAN_REQUIRED"
                    and str(receipt.get("reason") or "").strip().upper() == "CANONICAL_CONTRADICTION"
                    and command.project.standing_authority
                    and not canonical_repair_attempted
                ):
                    canonical_repair_attempted = True
                    store.append_event(command_id, "canonical.reconciliation_started", {
                        "cycle": cycle,
                        "policy": "BOUNDED_MISSING_EXECUTION_BASE_ONLY",
                        "production": "NO_GO",
                    })
                    repair = reconcile_missing_execution_base(
                        descriptor_path=Path(command.project.descriptor_path),
                        product_workspace=Path(command.project.workspace),
                        runtime_dir=project_runtime,
                    )
                    store.append_event(command_id, "canonical.reconciliation_result", {
                        "status": repair.get("status"),
                        "reason": repair.get("reason"),
                        "control_sha_before": repair.get("control_sha_before"),
                        "control_sha_after": repair.get("control_sha_after"),
                        "accepted_execution_base_sha": repair.get("accepted_execution_base_sha"),
                        "push_mode": repair.get("push_mode"),
                        "frontier_injected": False,
                        "run_plan_injected": False,
                    })
                    if repair.get("status") == "APPLIED":
                        receipt = {}
                        store.write_state(
                            command_id,
                            state="RUNNING",
                            disposition="CANONICAL_RECONCILIATION_APPLIED",
                            canonical_repair_control_sha=repair.get("control_sha_after"),
                            canonical_execution_base_sha=repair.get("accepted_execution_base_sha"),
                            worker_pid=os.getpid(),
                        )
                        continue
                    receipt = dict(receipt)
                    receipt["canonical_reconciliation"] = repair

                if disposition in ("WAITING_FOR_CI", "DELIVERING"):
                    candidate_sha = receipt.get("candidate_sha")
                    ci_workflow = receipt.get("ci_workflow_identity") or "lari-ci.yml"
                    store.write_state(
                        command_id,
                        state=_terminal_state(disposition),
                        disposition=disposition,
                        completed_batch_count=completed,
                        canonical_source_sha=receipt.get("canonical_source_sha"),
                        canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                        worker_pid=None,
                        candidate_sha=candidate_sha,
                        ci_workflow_identity=ci_workflow,
                        delivery_stage=disposition,
                        retry_after_epoch=time.time() + 15.0 if disposition == "WAITING_FOR_CI" else 0,
                        source_transport_failure_count=0,
                        failure_class=None,
                        error_class=None,
                        error=None,
                    )
                    store.append_event(command_id, "continuation.cycle_result", {
                        "cycle": cycle,
                        "disposition": disposition,
                        "completed_batch_count": completed,
                        "candidate_sha": candidate_sha,
                    })
                    return {
                        "command_id": command_id,
                        "disposition": disposition,
                        "state": disposition,
                        "candidate_sha": candidate_sha,
                        "completed_batch_count": completed,
                    }

                store.write_state(
                    command_id,
                    state=_terminal_state(disposition),
                    disposition=disposition,
                    completed_batch_count=completed,
                    canonical_source_sha=receipt.get("canonical_source_sha"),
                    canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                    worker_pid=os.getpid(),
                    retry_after_epoch=0,
                    source_transport_failure_count=0,
                    failure_class=None,
                    error_class=None,
                    error=None,
                )
                store.append_event(command_id, "continuation.cycle_result", {
                    "cycle": cycle,
                    "disposition": disposition,
                    "completed_batch_count": completed,
                    "canonical_source_sha": receipt.get("canonical_source_sha"),
                })

                if disposition == "BOUNDED_RUN_EXHAUSTED" and command.continuous:
                    # An already in-flight cycle may finish after PAUSED_SAFE is
                    # persisted, but continuous execution must not begin a new
                    # cycle while maintenance is active.
                    if not CommandAdmissionStore(runtime_root).is_active(command_id):
                        return _admission_hold_cycle_result(store, command_id, cycle=cycle)
                    if is_paused(runtime_root):
                        return _pause_safe_cycle_result(
                            store,
                            command_id,
                            project_runtime,
                            cycle=cycle,
                            receipt=receipt,
                        )

                    checkpoint = read_json(project_runtime / "planning-kernel-checkpoint.json") or {}
                    fingerprint = build_recovery_fingerprint(
                        project_id=command.project.project_id,
                        state={
                            **store.read_state(command_id),
                            "failure_class": "PLANNER_VALIDATION",
                            "completed_batch_count": completed,
                        },
                        checkpoint={
                            **checkpoint,
                            "phase": "BOUNDED_RUN_EXHAUSTED",
                        },
                    )
                    current_state = store.read_state(command_id)
                    previous_fingerprint = str(
                        current_state.get("recovery_fingerprint_sha256") or ""
                    )
                    repeats = (
                        int(current_state.get("same_fingerprint_respawns", 0) or 0) + 1
                        if previous_fingerprint == fingerprint["fingerprint_sha256"]
                        else 1
                    )
                    if repeats >= 3:
                        is_provider_wait = (
                            fingerprint.get("failure_family") == "PROVIDER_OUTAGE"
                            or "WAITING_FOR_REASONING_PROVIDER" in str(checkpoint.get("phase") or checkpoint.get("disposition") or "").upper()
                            or "WAITING_FOR_REASONING_PROVIDER" in str(current_state.get("disposition") or current_state.get("state") or "").upper()
                        )
                        if is_provider_wait:
                            # Resource unavailability alone MUST NOT require human decision.
                            # Keep state WAITING_FOR_REASONING_PROVIDER with bounded backoff and preserve automatic re-entry.
                            store.write_state(
                                command_id,
                                state="WAITING_FOR_REASONING_PROVIDER",
                                disposition="WAITING_FOR_RESOURCE",
                                failure_class="PROVIDER_RESOURCE_WAIT",
                                recovery_disposition="WAITING_FOR_RESOURCE",
                                recovery_fingerprint=fingerprint,
                                recovery_fingerprint_sha256=fingerprint["fingerprint_sha256"],
                                completed_count_baseline=fingerprint["completed_batch_count_baseline"],
                                same_fingerprint_respawns=repeats,
                                strategy_generation=strategy_generation,
                                last_worker_exit_code=0,
                                last_recovery_at=utc_now(),
                                worker_pid=None,
                                retry_after_epoch=time.time() + 60.0,
                            )
                            store.append_event(command_id, "runtime.resource_wait_held", {
                                "reason": "WAITING_FOR_RESOURCE",
                                "fingerprint": fingerprint,
                                "same_fingerprint_respawns": repeats,
                                "lineage_preserved": True,
                                "human_action_required": False,
                            })
                            result = RuntimeResult(
                                command_id=command_id,
                                state="WAITING_FOR_REASONING_PROVIDER",
                                disposition="WAITING_FOR_RESOURCE",
                                completed_batch_count=completed,
                                canonical_source_sha=receipt.get("canonical_source_sha"),
                                canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                                receipt={
                                    **receipt,
                                    "reason": "WAITING_FOR_RESOURCE",
                                    "recovery_fingerprint": fingerprint,
                                },
                            ).to_dict()
                            store.write_result(command_id, result)
                            return result

                        store.write_state(
                            command_id,
                            state="TECHNICAL_HOLD",
                            disposition="TECHNICAL_HOLD",
                            failure_class="REPLAN_NOOP",
                            recovery_disposition="BOUNDED_NON_CONVERGENCE_HOLD",
                            recovery_fingerprint=fingerprint,
                            recovery_fingerprint_sha256=fingerprint["fingerprint_sha256"],
                            completed_count_baseline=fingerprint["completed_batch_count_baseline"],
                            same_fingerprint_respawns=repeats,
                            strategy_generation=strategy_generation,
                            last_worker_exit_code=0,
                            last_recovery_at=utc_now(),
                            worker_pid=None,
                            retry_after_epoch=0,
                        )
                        store.append_event(command_id, "runtime.recovery_churn_held", {
                            "reason": "REPLAN_NOOP",
                            "fingerprint": fingerprint,
                            "same_fingerprint_respawns": repeats,
                            "lineage_preserved": True,
                        })
                        result = RuntimeResult(
                            command_id=command_id,
                            state="TECHNICAL_HOLD",
                            disposition="TECHNICAL_HOLD",
                            completed_batch_count=completed,
                            canonical_source_sha=receipt.get("canonical_source_sha"),
                            canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                            receipt={
                                **receipt,
                                "reason": "REPLAN_NOOP",
                                "recovery_fingerprint": fingerprint,
                            },
                        ).to_dict()
                        store.write_result(command_id, result)
                        return result
                    if repeats == 2:
                        strategy_generation += 1
                        recovery_failure_context = fingerprint
                        recovery_disposition = "STRATEGY_ESCALATED"
                        store.append_event(command_id, "runtime.recovery_strategy_escalated", {
                            "fingerprint": fingerprint,
                            "strategy_generation": strategy_generation,
                            "same_fingerprint_respawns": repeats,
                        })
                    else:
                        recovery_disposition = "NORMAL_RESUME"
                    store.write_state(
                        command_id,
                        recovery_disposition=recovery_disposition,
                        recovery_fingerprint=fingerprint,
                        recovery_fingerprint_sha256=fingerprint["fingerprint_sha256"],
                        completed_count_baseline=fingerprint["completed_batch_count_baseline"],
                        same_fingerprint_respawns=repeats,
                        strategy_generation=strategy_generation,
                        last_worker_exit_code=0,
                        last_recovery_at=utc_now(),
                    )

                    # No routine user prompt. Re-enter the planning kernel from its
                    # durable checkpoint; it fresh-reads canonical state and replans.
                    continue

                if disposition == "WAITING_FOR_REASONING_PROVIDER":
                    circuit_reg = ProviderCircuitBreakerRegistry(project_runtime / "provider-circuits.json")
                    quota_retry = receipt.get("quota_retry_after_epoch")
                    retry_at = (
                        float(quota_retry)
                        if quota_retry is not None
                        else circuit_reg.earliest_next_probe()
                    )
                    # Command attempts count real worker execution attempts; do not inflate during provider outage probe loops
                    final_attempts = initial_attempts
                    store.write_state(
                        command_id,
                        state="WAITING_FOR_REASONING_PROVIDER",
                        worker_pid=None,
                        attempts=final_attempts,
                        retry_after_epoch=retry_at,
                        required_task_class=receipt.get(
                            "required_task_class", "structured_planning"
                        ),
                        quota_key=receipt.get("quota_key"),
                    )
                    store.append_event(command_id, "run.waiting_for_reasoning_provider", {
                        "retry_after_epoch": retry_at,
                        "attempt": final_attempts,
                        "circuit_summary": circuit_reg.summarize(),
                        "required_task_class": receipt.get(
                            "required_task_class", "structured_planning"
                        ),
                        "quota_key": receipt.get("quota_key"),
                    })
                    result = RuntimeResult(
                        command_id=command_id,
                        state="WAITING_FOR_REASONING_PROVIDER",
                        disposition=disposition,
                        completed_batch_count=completed,
                        canonical_source_sha=receipt.get("canonical_source_sha"),
                        canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                        receipt=receipt,
                    ).to_dict()
                    store.write_result(command_id, result)
                    return result

                kcp_receipt = None
                kcp_error_classification = None
                kcp_error_message = None
                try:
                    kcp_receipt = _record_kcp_verification_receipt(
                        runtime_root,
                        project_id=command.project.project_id,
                        command_id=command_id,
                        receipt=receipt,
                        project_runtime=project_runtime,
                    )
                except Exception as kcp_exc:
                    # CORRECTION C: Fail-closed KCP errors
                    # Never silently swallow ledger write failures at verified-delivery boundary
                    kcp_error_classification = "KCP_RECEIPT_WRITE_FAILED"
                    kcp_error_message = _safe_exception_message(kcp_exc)

                state = _terminal_state(disposition)
                result_payload = dict(receipt)
                if kcp_receipt is not None:
                    result_payload["kcp_verification_receipt"] = {
                        "event_id": kcp_receipt.get("event_id"),
                        "idempotency_key": kcp_receipt.get("idempotency_key"),
                        "sequence": kcp_receipt.get("sequence"),
                    }
                elif kcp_error_classification is not None:
                    result_payload["kcp_error"] = {
                        "classification": kcp_error_classification,
                        "error": kcp_error_message,
                    }

                result = RuntimeResult(
                    command_id=command_id,
                    state=state,
                    disposition=disposition or "FAILED",
                    completed_batch_count=completed,
                    canonical_source_sha=receipt.get("canonical_source_sha"),
                    canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                    receipt=result_payload,
                ).to_dict()
                store.write_result(command_id, result)

                state_kwargs: Dict[str, Any] = {
                    "state": state,
                    "worker_pid": None,
                }
                if kcp_error_classification is not None:
                    state_kwargs["kcp_verification_status"] = "FAILED"
                    state_kwargs["kcp_error"] = kcp_error_classification
                elif kcp_receipt is not None:
                    state_kwargs["kcp_verification_status"] = "PASS"
                    state_kwargs["kcp_receipt_id"] = kcp_receipt.get("event_id")

                store.write_state(command_id, **state_kwargs)
                event_payload = {
                    "state": state,
                    "disposition": result["disposition"],
                    "completed_batch_count": completed,
                }
                if kcp_receipt is not None:
                    event_payload["kcp_receipt_id"] = kcp_receipt.get("event_id")
                elif kcp_error_classification is not None:
                    event_payload["kcp_error"] = kcp_error_classification
                store.append_event(command_id, "run.finished", event_payload)
                return result
        except BaseException as exc:
            # Hard termination bypasses this block and is recovered from the durable
            # checkpoint. Ordinary failures always become structured Runtime V1 IPC.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            message = _safe_exception_message(exc)

            if _is_transient_source_transport_error(exc):
                checkpoint = (
                    read_json(
                        project_runtime
                        / "planning-kernel-checkpoint.json"
                    )
                    or {}
                )

                completed = (
                    cumulative_completed_batch_count(
                        checkpoint
                    )
                )

                previous_state = (
                    store.read_state(
                        command_id
                    )
                    or {}
                )

                failure_count = (
                    int(
                        previous_state.get(
                            "source_transport_failure_count",
                            0,
                        )
                        or 0
                    )
                    + 1
                )

                retry_delay = min(
                    600.0,
                    30.0
                    * (
                        2
                        **
                        min(
                            failure_count - 1,
                            4,
                        )
                    ),
                )

                retry_after = (
                    time.time()
                    + retry_delay
                )

                failure_receipt = {
                    "reason":
                        message,

                    "failure_class":
                        "SOURCE_TRANSPORT_UNAVAILABLE",

                    "error_class":
                        exc.__class__.__name__,

                    "structured_runtime_failure":
                        True,

                    "retry_after_epoch":
                        retry_after,

                    "lineage_preserved":
                        True,
                }

                result = RuntimeResult(
                    command_id=command_id,
                    state="WAITING_FOR_SOURCE_TRANSPORT",
                    disposition="WAITING_FOR_SOURCE_TRANSPORT",
                    completed_batch_count=completed,
                    canonical_source_sha=checkpoint.get(
                        "canonical_source_sha"
                    ),
                    canonical_execution_base_sha=checkpoint.get(
                        "canonical_execution_base_sha"
                    ),
                    receipt=failure_receipt,
                ).to_dict()

                store.write_result(
                    command_id,
                    result,
                )

                store.write_state(
                    command_id,
                    state="WAITING_FOR_SOURCE_TRANSPORT",
                    disposition="WAITING_FOR_SOURCE_TRANSPORT",
                    worker_pid=None,
                    error_class=exc.__class__.__name__,
                    failure_class="SOURCE_TRANSPORT_UNAVAILABLE",
                    error=message,
                    completed_batch_count=completed,
                    retry_after_epoch=retry_after,
                    source_transport_failure_count=failure_count,
                )

                stop.set()

                if watcher is not None:
                    watcher.join(
                        timeout=2.0
                    )
                    watcher = None

                store.append_event(
                    command_id,
                    "run.waiting_for_source_transport",
                    {
                        "error_class":
                            exc.__class__.__name__,

                        "failure_class":
                            "SOURCE_TRANSPORT_UNAVAILABLE",

                        "retry_after_epoch":
                            retry_after,

                        "retry_delay_seconds":
                            retry_delay,

                        "source_transport_failure_count":
                            failure_count,

                        "completed_batch_count":
                            completed,

                        "lineage_preserved":
                            True,
                    },
                )

                return result

            state, failure_class = _classify_exception(exc)
            failure_receipt = {
                "reason": message,
                "failure_class": failure_class,
                "error_class": exc.__class__.__name__,
                "structured_runtime_failure": True,
            }
            if state == "TECHNICAL_HOLD":
                failure_receipt["lineage_preserved"] = True
                if isinstance(exc, BackendLocalReasoningFailure):
                    failure_receipt["backend_failure_class"] = exc.failure_class
                    failure_receipt["required_task_class"] = exc.task_class
            checkpoint = read_json(project_runtime / "planning-kernel-checkpoint.json")
            completed = cumulative_completed_batch_count(checkpoint)
            result = RuntimeResult(
                command_id=command_id,
                state=state,
                disposition=state,
                completed_batch_count=completed,
                canonical_source_sha=checkpoint.get("canonical_source_sha"),
                canonical_execution_base_sha=checkpoint.get("canonical_execution_base_sha"),
                receipt=failure_receipt,
            ).to_dict()
            store.write_result(command_id, result)
            store.write_state(
                command_id,
                state=state,
                disposition=state,
                worker_pid=None,
                error_class=exc.__class__.__name__,
                failure_class=failure_class,
                error=message,
                completed_batch_count=completed,
            )
            # A terminal event must be the final event for this worker attempt.
            # Stop telemetry before publishing it so a recovery watcher cannot
            # replay an older WAITING checkpoint after run.failed.
            stop.set()
            if watcher is not None:
                watcher.join(timeout=2.0)
                watcher = None
            event_type = (
                "run.human_required"
                if state == "HUMAN_REQUIRED"
                else (
                    "run.technical_hold"
                    if state == "TECHNICAL_HOLD"
                    else "run.failed"
                )
            )
            store.append_event(command_id, event_type, {
                "error_class": exc.__class__.__name__,
                "failure_class": failure_class,
                "message": message[:1000],
                "lineage_preserved": state == "TECHNICAL_HOLD",
            })
            if state in ("HUMAN_REQUIRED", "TECHNICAL_HOLD"):
                return result
            raise
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=2.0)
            if ws_lock_ctx is not None:
                try:
                    ws_lock_ctx.__exit__(None, None, None)
                except Exception:
                    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Runtime V1 detached command worker")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--command-id", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        execute_command(Path(args.runtime_root), args.command_id)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Orchestration consumers read structured state/result/events, not stdout.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
