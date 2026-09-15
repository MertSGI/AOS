"""Detached command worker for AOS Runtime V1.

The worker never treats stdout as the orchestration contract. Command state,
results, and events are durable structured JSON/JSONL artifacts under the
Runtime V1 root.  On restart the planning kernel resumes from its own durable
checkpoint before new reasoning is invoked.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from aos.planning_kernel import DEFAULT_RED_LINES, run_autonomous_project
from aos.runtime_contract import ContinueProjectCommand, RuntimeResult, utc_now
from aos.runtime_store import RuntimeStore, exclusive_file_lock, read_json
from aos.secure_store import hydrate_environment


class PlanningArtifactWatcher(threading.Thread):
    def __init__(self, store: RuntimeStore, command_id: str, project_runtime: Path, stop: threading.Event) -> None:
        super().__init__(name=f"aos-artifact-watcher-{command_id}", daemon=True)
        self.store = store
        self.command_id = command_id
        self.project_runtime = project_runtime
        self.stop_event = stop
        self.seen: Set[str] = set()
        self.last_checkpoint_signature: Optional[str] = None

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
        signature = json.dumps({
            "phase": data.get("phase"),
            "batch_number": data.get("batch_number"),
            "completed_batch_count": len(data.get("completed_batches", []) or []),
            "replan_reason": data.get("replan_reason"),
            "canonical_source_sha": data.get("canonical_source_sha"),
        }, sort_keys=True)
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
            "completed_batch_count": len(data.get("completed_batches", []) or []),
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


def _terminal_state(disposition: str) -> str:
    if disposition == "PROJECT_COMPLETE":
        return "PROJECT_COMPLETE"
    if disposition == "HUMAN_REQUIRED":
        return "HUMAN_REQUIRED"
    if disposition == "WAITING_FOR_REASONING_PROVIDER":
        return "WAITING_FOR_REASONING_PROVIDER"
    if disposition == "BOUNDED_RUN_EXHAUSTED":
        return "RUNNING"
    return "FAILED"


def execute_command(runtime_root: Path, command_id: str) -> Dict[str, Any]:
    store = RuntimeStore(runtime_root)
    raw = store.read_command(command_id)
    if not raw:
        raise ValueError(f"Command not found: {command_id}")
    command = ContinueProjectCommand.from_mapping(raw)
    command_root = store.command_dir(command_id)
    project_runtime = command_root / "project-runtime"
    project_runtime.mkdir(parents=True, exist_ok=True)

    worker_lock = command_root / "worker.lock"
    with exclusive_file_lock(worker_lock):
        previous = store.read_state(command_id)
        attempts = int(previous.get("attempts", 0)) + 1
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
            checkpoint = read_json(project_runtime / "planning-kernel-checkpoint.json")
            store.append_event(command_id, "runtime.worker_recovered", {
                "checkpoint_phase": checkpoint.get("phase"),
                "batch_number": checkpoint.get("batch_number"),
                "completed_batch_count": len(checkpoint.get("completed_batches", []) or []),
            })

        hydrate_environment(overwrite=True)
        stop = threading.Event()
        watcher = PlanningArtifactWatcher(store, command_id, project_runtime, stop)
        watcher.start()
        receipt: Dict[str, Any] = {}
        cycle = 0
        try:
            while True:
                cycle += 1
                store.append_event(command_id, "continuation.cycle_started", {
                    "cycle": cycle,
                    "max_batches_per_cycle": command.max_batches_per_cycle,
                })
                receipt = run_autonomous_project(
                    descriptor_path=Path(command.project.descriptor_path),
                    workspace=Path(command.project.workspace),
                    runtime_dir=project_runtime,
                    routing_policy_path=Path(command.project.routing_policy_path),
                    goal=command.goal,
                    constraints=command.constraints,
                    red_lines=command.red_lines or tuple(DEFAULT_RED_LINES),
                    max_batches=command.max_batches_per_cycle,
                    max_iterations_per_batch=command.max_iterations_per_batch,
                )
                disposition = str(receipt.get("disposition", ""))
                completed = int(receipt.get("completed_batch_count", 0) or 0)
                store.write_state(
                    command_id,
                    state=_terminal_state(disposition),
                    disposition=disposition,
                    completed_batch_count=completed,
                    canonical_source_sha=receipt.get("canonical_source_sha"),
                    canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                    worker_pid=os.getpid(),
                )
                store.append_event(command_id, "continuation.cycle_result", {
                    "cycle": cycle,
                    "disposition": disposition,
                    "completed_batch_count": completed,
                    "canonical_source_sha": receipt.get("canonical_source_sha"),
                })

                if disposition == "BOUNDED_RUN_EXHAUSTED" and command.continuous:
                    # No routine user prompt. Re-enter the planning kernel from its
                    # durable checkpoint; it fresh-reads canonical state and replans.
                    continue

                if disposition == "WAITING_FOR_REASONING_PROVIDER":
                    retry_at = time.time() + 300
                    store.write_state(
                        command_id,
                        state="WAITING_FOR_REASONING_PROVIDER",
                        worker_pid=None,
                        retry_after_epoch=retry_at,
                    )
                    store.append_event(command_id, "run.waiting_for_reasoning_provider", {
                        "retry_after_epoch": retry_at,
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

                state = _terminal_state(disposition)
                result = RuntimeResult(
                    command_id=command_id,
                    state=state,
                    disposition=disposition or "FAILED",
                    completed_batch_count=completed,
                    canonical_source_sha=receipt.get("canonical_source_sha"),
                    canonical_execution_base_sha=receipt.get("canonical_execution_base_sha"),
                    receipt=receipt,
                ).to_dict()
                store.write_result(command_id, result)
                store.write_state(command_id, state=state, worker_pid=None)
                store.append_event(command_id, "run.finished", {
                    "state": state,
                    "disposition": result["disposition"],
                    "completed_batch_count": completed,
                })
                return result
        except BaseException as exc:
            # SystemExit/KeyboardInterrupt are included because a detached worker may
            # be terminated externally during restart proof. A hard process kill will
            # skip this block; server recovery handles that case from durable state.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            store.write_state(
                command_id,
                state="FAILED",
                worker_pid=None,
                error_class=exc.__class__.__name__,
                error=str(exc)[:1500],
            )
            store.append_event(command_id, "run.failed", {
                "error_class": exc.__class__.__name__,
                "message": str(exc)[:1000],
            })
            raise
        finally:
            stop.set()
            watcher.join(timeout=2.0)


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
