"""Loopback Runtime API V1 for AOS.

AOS Direct and the CLI both use this API. The server persists commands before
spawning detached workers and recovers unfinished work after process restarts.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from aos.runtime_contract import (
    CONTRACT_VERSION,
    ContinueProjectCommand,
    ProjectProfile,
    resolve_project,
    resolve_under_authorized_roots,
    utc_now,
    validate_configured_project_profiles,
    validate_project_profile_paths,
    validate_runtime_config,
)
from aos.runtime_store import RuntimeStore, atomic_json, read_json
from aos.process_utils import OwnedProcess, popen_headless, process_alive, terminate_process_tree, get_headless_creationflags
from aos.controller_relay import AsyncControllerRelay, ControllerRelayPublisher
from aos.runtime_maintenance import PAUSED_SAFE, is_paused, persist_maintenance
from aos.provider_circuit import CircuitState, ProviderCircuitBreakerRegistry
from aos.provider_probe import probe_enabled_providers
from aos.secure_store import credential_is_configured, provider_presence

MAX_BODY_BYTES = 64 * 1024


def load_config(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Runtime config must be a JSON object")
    return validate_configured_project_profiles(value)


def pid_alive(pid: Any) -> bool:
    return process_alive(pid)


def _creationflags() -> int:
    return get_headless_creationflags(detached=True)


def _resolve_worker_executable() -> str:
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        candidate = Path(py).with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return py


def _build_worker_env(slot_root: Optional[str] = None) -> Dict[str, str]:
    env = dict(os.environ)
    site_dirs: list[str] = []
    if slot_root:
        candidate_site = Path(slot_root) / "site"
        if candidate_site.is_dir():
            site_dirs.append(str(candidate_site.resolve()))
    module_parent = Path(__file__).resolve().parent.parent
    if not slot_root and module_parent.is_dir():
        site_dirs.append(str(module_parent))
    all_parts = [p for p in site_dirs if p]
    if all_parts:
        env["PYTHONPATH"] = os.pathsep.join(all_parts)
    else:
        env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


class RuntimeEngine:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = validate_runtime_config(config)
        self.runtime_root = Path(self.config["runtime_root"])
        self.store = RuntimeStore(self.runtime_root)
        self.stop_event = threading.Event()
        self._pause_lock = threading.RLock()
        # This read occurs before any recovery/probe/telemetry thread exists.
        self.is_paused = is_paused(self.runtime_root)
        self._worker_processes: Dict[str, OwnedProcess] = {}
        self._status_lock = threading.Lock()
        self._status_cache: Dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status_state": "PAUSED" if self.is_paused else "INITIALIZING",
            "timestamp": utc_now(),
            "production": "NO_GO",
        }
        relay_dir = Path(self.config.get("controller_relay_dir") or "C:/Projects/AOS/.aos-runtime/controller-relay")
        self.publisher = ControllerRelayPublisher(relay_dir, self.config, writer_instance_id=f"aos-api-{os.getpid()}")
        self.relay_worker = AsyncControllerRelay(self.publisher)
        self.recovery_thread = threading.Thread(target=self._recovery_loop, name="aos-runtime-recovery", daemon=True)
        self.recovery_thread.start()
        self.probe_thread: Optional[threading.Thread] = None
        self.telemetry_thread = threading.Thread(target=self._status_loop, name="aos-runtime-status-cache", daemon=True)
        self.telemetry_thread.start()
        if not self.is_paused:
            self._start_probe_thread()

    def _start_probe_thread(self) -> None:
        if self.is_paused or (self.probe_thread is not None and self.probe_thread.is_alive()):
            return
        self.probe_thread = threading.Thread(
            target=self._run_live_probe_cycle,
            name="aos-runtime-provider-probe",
            daemon=True,
        )
        self.probe_thread.start()

    @staticmethod
    def _enabled_from_policy(policy_path: Path) -> list[str]:
        try:
            value = json.loads(policy_path.read_text(encoding="utf-8"))
            providers = value.get("providers", {}) if isinstance(value, dict) else {}
            return [
                str(provider_id)
                for provider_id, provider in providers.items()
                if isinstance(provider, dict) and provider.get("enabled") is True
            ]
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _provider_evidence(self) -> tuple[list[str], list[Path], list[ProviderCircuitBreakerRegistry]]:
        """Discover the exact command-local registries used by active/waiting workers."""
        enabled: list[str] = []
        paths: list[Path] = []
        registries: list[ProviderCircuitBreakerRegistry] = []
        for command_id in self.store.list_command_ids()[-200:]:
            state = self.store.read_state(command_id)
            if str(state.get("state") or "") not in (
                "QUEUED", "RUNNING", "RECOVERING", "WAITING_FOR_REASONING_PROVIDER"
            ):
                continue
            command = self.store.read_command(command_id)
            policy_value = (command.get("project") or {}).get("routing_policy_path")
            if policy_value:
                for provider_id in self._enabled_from_policy(Path(str(policy_value))):
                    if provider_id not in enabled:
                        enabled.append(provider_id)
            path = self.store.command_dir(command_id) / "project-runtime" / "provider-circuits.json"
            paths.append(path)
            registries.append(ProviderCircuitBreakerRegistry(path))

        # Before a command exists, configured project policies still define the
        # enabled set. They do not create provider health evidence.
        if not enabled:
            for project in (self.config.get("projects") or {}).values():
                policy_value = project.get("routing_policy_path") if isinstance(project, dict) else None
                if policy_value:
                    for provider_id in self._enabled_from_policy(Path(str(policy_value))):
                        if provider_id not in enabled:
                            enabled.append(provider_id)
        return enabled, paths, registries

    def _run_live_probe_cycle(self) -> None:
        """Perform one sanitized activation probe and wake same waiting lineages."""
        try:
            targets: Dict[str, Dict[str, Any]] = {}
            for command_id in self.store.list_command_ids()[-200:]:
                state = self.store.read_state(command_id)
                if str(state.get("state") or "") not in (
                    "QUEUED", "RUNNING", "RECOVERING", "WAITING_FOR_REASONING_PROVIDER"
                ):
                    continue
                command = self.store.read_command(command_id)
                policy_value = (command.get("project") or {}).get("routing_policy_path")
                if not policy_value:
                    continue
                policy_path = Path(str(policy_value))
                key = str(policy_path.resolve())
                targets.setdefault(key, {"path": policy_path, "commands": []})["commands"].append(command_id)

            any_success = False
            for target in targets.values():
                results = probe_enabled_providers(target["path"])
                any_success = any_success or any(
                    row.get("probe_status") == "PASS" for row in results.values()
                )
                for command_id in target["commands"]:
                    registry = ProviderCircuitBreakerRegistry(
                        self.store.command_dir(command_id) / "project-runtime" / "provider-circuits.json"
                    )
                    for provider_id, row in results.items():
                        if row.get("probe_attempted"):
                            registry.record_probe(provider_id)
                        common = {
                            "observed_at": row.get("last_observed_at"),
                            "probe_status": str(row.get("probe_status") or "UNKNOWN"),
                            "latency_ms": row.get("latency_ms"),
                            "credential_available": row.get("credential_available"),
                            "local_service_available": row.get("local_service_available"),
                        }
                        if row.get("probe_status") == "PASS":
                            registry.record_success(provider_id, **common)
                        else:
                            registry.record_failure(
                                provider_id,
                                str(row.get("failure_class") or "UNKNOWN"),
                                **common,
                            )
                    self.store.append_event(command_id, "provider.activation_probe_completed", {
                        "providers": list(results.values()),
                        "secrets_exposed": False,
                    })

            if any_success and not self.is_paused:
                for command_id in self.store.list_command_ids()[-200:]:
                    state = self.store.read_state(command_id)
                    if str(state.get("state") or "") != "WAITING_FOR_REASONING_PROVIDER":
                        continue
                    self.store.write_state(command_id, retry_after_epoch=0)
                    self.store.append_event(command_id, "provider.healthy_alternate_wake", {
                        "lineage_preserved": True,
                        "command_id": command_id,
                    })
        except Exception:
            # Probe telemetry is fail-safe and must never terminate the runtime API.
            return

    def _normalize_project(self, project_id: Optional[str]) -> ProjectProfile:
        profile = resolve_project(self.config, project_id)
        roots = self.config["authorized_roots"]
        descriptor = resolve_under_authorized_roots(profile.descriptor_path, roots)
        workspace = resolve_under_authorized_roots(profile.workspace, roots)
        policy = resolve_under_authorized_roots(profile.routing_policy_path, roots)
        if not descriptor.is_file():
            raise ValueError(f"Project descriptor missing: {descriptor}")
        if not workspace.is_dir():
            raise ValueError(f"Project workspace missing: {workspace}")
        if not policy.is_file():
            raise ValueError(f"Routing policy missing: {policy}")
        normalized = ProjectProfile(
            project_id=profile.project_id,
            descriptor_path=str(descriptor),
            workspace=str(workspace),
            routing_policy_path=str(policy),
            standing_authority=profile.standing_authority,
        )
        validate_project_profile_paths(normalized)
        return normalized

    def submit_continue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._pause_lock:
            if self.is_paused:
                raise RuntimeError("Runtime is paused-safe; resume before submitting a new goal")
            profile = self._normalize_project(payload.get("project_id"))
            command = ContinueProjectCommand.from_mapping(payload, project=profile)
            self.store.create_command(command.to_dict())
            self._spawn_worker(command.command_id, recovered=False)
        return {
            "contract_version": CONTRACT_VERSION,
            "accepted": True,
            "command_id": command.command_id,
            "state": "QUEUED",
            "project_id": profile.project_id,
            "workspace": profile.workspace,
            "descriptor_path": profile.descriptor_path,
            "routing_policy_path": profile.routing_policy_path,
            "run_plan_required": False,
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }

    def _spawn_worker(self, command_id: str, *, recovered: bool) -> Optional[int]:
        with self._pause_lock:
            if self.is_paused:
                return None
            state = self.store.read_state(command_id)
            existing = state.get("worker_pid")
            if pid_alive(existing):
                return int(existing)
            command = self.store.read_command(command_id)
            if not command:
                return None
            target_state = "RECOVERING" if recovered else "QUEUED"
            self.store.write_state(command_id, state=target_state, worker_pid=None)
            cmd = [
                _resolve_worker_executable(),
                "-m",
                "aos.runtime_worker",
                "--runtime-root",
                str(self.runtime_root),
                "--command-id",
                command_id,
            ]
            slot_root = self.config.get("runtime_slot_root")
            worker_env = _build_worker_env(str(slot_root) if slot_root else None)
            proc = popen_headless(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                detached=True,
                env=worker_env,
            )
            self._worker_processes[command_id] = proc
            # The worker can reach RUNNING before Popen returns. Never downgrade a
            # concurrently advanced state back to QUEUED/RECOVERING.
            current = self.store.read_state(command_id)
            updates = {"worker_pid": proc.pid}
            if str(current.get("state")) not in ("RUNNING", "WAITING_FOR_REASONING_PROVIDER", "PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"):
                updates["state"] = target_state
            self.store.write_state(command_id, **updates)
            if recovered:
                self.store.append_event(command_id, "runtime.worker_respawned", {
                    "worker_pid": proc.pid,
                    "reason": "unfinished_command_recovery",
                })
            return proc.pid

    def _recover_one(self, command_id: str) -> None:
        if self.is_paused:
            return
        state = self.store.read_state(command_id)
        current = str(state.get("state") or "")
        if current in ("PROJECT_COMPLETE", "HUMAN_REQUIRED", "FAILED"):
            return
        if current == "WAITING_FOR_REASONING_PROVIDER":
            retry = float(state.get("retry_after_epoch", 0) or 0)
            if retry and time.time() < retry:
                return
        if pid_alive(state.get("worker_pid")):
            return
        self._spawn_worker(command_id, recovered=True)

    def recover_unfinished(self) -> None:
        for command_id in self.store.list_command_ids():
            try:
                self._recover_one(command_id)
            except Exception as exc:
                try:
                    self.store.append_event(command_id, "runtime.recovery_failed", {
                        "error_class": exc.__class__.__name__,
                        "message": str(exc)[:1000],
                    })
                except Exception:
                    pass

    def _wake_waiting_from_observed_provider_health(self) -> None:
        """Wake preserved lineages when newer command-local evidence is healthy.

        A successful inference or probe in one active lineage is authoritative
        provider evidence for the runtime.  The newest-event aggregation keeps a
        stale success from overriding a newer failure while avoiding a long
        backoff after another lineage has already proved the provider recovered.
        """
        if self.is_paused:
            return
        enabled, _paths, registries = self._provider_evidence()
        aggregate = ProviderCircuitBreakerRegistry.aggregate_registries(
            registries,
            enabled_providers=enabled,
        )
        details = aggregate.per_provider_details(enabled)
        healthy_details = [
            row for row in details
            if row.get("circuit_state") == CircuitState.CLOSED.value
        ]
        healthy = sorted(row["provider"] for row in healthy_details)
        if not healthy:
            return
        for command_id in self.store.list_command_ids()[-200:]:
            state = self.store.read_state(command_id)
            if str(state.get("state") or "") != "WAITING_FOR_REASONING_PROVIDER":
                continue
            if float(state.get("retry_after_epoch", 0) or 0) <= 0:
                continue
            # Copy the authoritative newest success into the waiting command's
            # own registry before waking it.  Otherwise the worker immediately
            # sees its stale all-open view, waits again, and recovery can churn.
            registry = ProviderCircuitBreakerRegistry(
                self.store.command_dir(command_id) / "project-runtime" / "provider-circuits.json"
            )
            for row in healthy_details:
                registry.record_success(
                    row["provider"],
                    observed_at=row.get("last_success_at") or row.get("last_observed_at"),
                    probe_status=str(row.get("probe_status") or "PASS"),
                    latency_ms=row.get("latency_ms"),
                    credential_available=row.get("credential_available"),
                    local_service_available=row.get("local_service_available"),
                )
            self.store.write_state(command_id, retry_after_epoch=0)
            self.store.append_event(command_id, "provider.healthy_alternate_wake", {
                "lineage_preserved": True,
                "command_id": command_id,
                "healthy_providers": healthy,
                "evidence_source": "NEWEST_COMMAND_LOCAL_OBSERVATION",
            })

    def _recovery_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.is_paused:
                self._wake_waiting_from_observed_provider_health()
                self.recover_unfinished()
            self.stop_event.wait(3.0)

    def health(self) -> Dict[str, Any]:
        """Constant-time liveness/readiness identity; never traverses runtime data."""
        return {
            "contract_version": CONTRACT_VERSION,
            "runtime_state": "HEALTHY",
            "maintenance_state": PAUSED_SAFE if self.is_paused else "RUNNING",
            "paused": bool(self.is_paused),
            "autonomous_spawning_enabled": not self.is_paused,
            "pid": os.getpid(),
            "timestamp": utc_now(),
            "runtime_source_sha": self.config.get("candidate_source_sha"),
            "runtime_asset_tree_sha256": self.config.get("runtime_asset_tree_sha256"),
            "runtime_slot_root": self.config.get("runtime_slot_root"),
            "runtime_slot_id": self.config.get("runtime_slot_id"),
            "runtime_launch_nonce": os.environ.get("AOS_RUNTIME_LAUNCH_NONCE"),
            "runtime_supervisor_pid": os.environ.get("AOS_RUNTIME_SUPERVISOR_PID"),
            "production": "NO_GO",
            "ag_backend_enabled": False,
        }

    def status(self) -> Dict[str, Any]:
        """Return only the last background-generated detailed telemetry snapshot."""
        with self._status_lock:
            return dict(self._status_cache)

    def _status_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.is_paused:
                try:
                    value = self._collect_detailed_status()
                    value["status_state"] = "READY"
                    with self._status_lock:
                        self._status_cache = value
                except Exception as exc:
                    with self._status_lock:
                        self._status_cache = {
                            "contract_version": CONTRACT_VERSION,
                            "status_state": "DEGRADED",
                            "error_class": exc.__class__.__name__,
                            "timestamp": utc_now(),
                            "production": "NO_GO",
                        }
            self.stop_event.wait(5.0)

    def _collect_detailed_status(self) -> Dict[str, Any]:
        """Slow bounded-history enrichment. Never executes in an HTTP request thread."""
        active = []
        waiting = []
        terminal = []
        active_by_project: Dict[str, List[str]] = {}
        command_ids = self.store.list_command_ids()[-200:]
        latest_summary = None
        for command_id in command_ids:
            state = self.store.read_state(command_id)
            current = str(state.get("state") or "UNKNOWN")
            cmd = self.store.read_command(command_id)
            proj_id = (cmd.get("project") or {}).get("project_id") or self.config["default_project"]
            if current in ("QUEUED", "RUNNING", "RECOVERING"):
                active.append(command_id)
                active_by_project.setdefault(proj_id, []).append(command_id)
            elif current == "WAITING_FOR_REASONING_PROVIDER":
                waiting.append(command_id)
            else:
                terminal.append(command_id)
        if command_ids:
            latest_id = command_ids[-1]
            latest_state = self.store.read_state(latest_id)
            latest_summary = {
                "command_id": latest_id,
                "state": latest_state.get("state"),
                "disposition": latest_state.get("disposition"),
                "completed_batch_count": int(latest_state.get("completed_batch_count", 0) or 0),
                "failure_class": latest_state.get("failure_class"),
                "canonical_source_sha": latest_state.get("canonical_source_sha"),
            }
        # Aggregate the same command-local registries workers persist. No global
        # provider registry is created or consulted.
        enabled_providers, circuit_paths, registries = self._provider_evidence()
        circuit_reg = ProviderCircuitBreakerRegistry.aggregate_registries(
            registries,
            enabled_providers=enabled_providers,
        )
        circuit_summary = circuit_reg.summarize(enabled_providers)
        presence = provider_presence()
        credential_status = {
            pid: credential_is_configured(pid, presence=presence)
            for pid in enabled_providers
        }

        provider_details = circuit_reg.per_provider_details(enabled_providers, credential_status)
        for row in provider_details:
            row["provider_id"] = row.pop("provider")
            row["probe_attempted"] = bool(row.get("probe_count"))
            row["failure_class"] = row.get("last_failure_class")
        healthy = [row for row in provider_details if row.get("circuit_state") == CircuitState.CLOSED.value]
        selected_provider = None
        if healthy:
            selected_provider = max(
                healthy,
                key=lambda row: str(row.get("last_success_at") or ""),
            ).get("provider_id")
        return {
            "contract_version": CONTRACT_VERSION,
            "runtime_state": "HEALTHY",
            "paused": bool(self.is_paused),
            "autonomous_spawning_enabled": not self.is_paused,
            "pid": os.getpid(),
            "timestamp": utc_now(),
            "active_commands": active,
            "active_commands_by_project": active_by_project,
            "registered_projects": list((self.config.get("projects") or {}).keys()),
            "waiting_commands": waiting,
            "terminal_command_count": len(terminal),
            "latest_command": latest_summary,
            "default_project": self.config["default_project"],
            "runtime_source_sha": self.config.get("candidate_source_sha"),
            "runtime_asset_tree_sha256": self.config.get("runtime_asset_tree_sha256"),
            "runtime_slot_root": self.config.get("runtime_slot_root"),
            "runtime_slot_id": self.config.get("runtime_slot_id"),
            "runtime_launch_nonce": os.environ.get("AOS_RUNTIME_LAUNCH_NONCE"),
            "runtime_supervisor_pid": os.environ.get("AOS_RUNTIME_SUPERVISOR_PID"),
            "production": "NO_GO",
            "ag_backend_enabled": False,
            "healthy_reasoning_provider_count": circuit_summary["healthy_reasoning_provider_count"],
            "probe_eligible_reasoning_provider_count": circuit_summary["probe_eligible_reasoning_provider_count"],
            "unknown_reasoning_provider_count": circuit_summary["unknown_reasoning_provider_count"],
            "provider_circuits_open": circuit_summary["provider_circuits_open"],
            "all_reasoning_providers_unavailable": circuit_summary["all_reasoning_providers_unavailable"],
            "next_provider_probe_at": circuit_summary["next_provider_probe_at"],
            "last_provider_success": circuit_summary["last_provider_success"],
            "provider_probe_count": circuit_summary["provider_probe_count"],
            "provider_failover_count": circuit_summary["provider_failover_count"],
            "provider_details": provider_details,
            "enabled_reasoning_providers": enabled_providers,
            "healthy_reasoning_providers": [row["provider_id"] for row in healthy],
            "current_selected_reasoning_provider": selected_provider,
            "provider_circuit_registry_paths": [str(path) for path in circuit_paths],
        }

    def pause_safe(self) -> Dict[str, Any]:
        with self._pause_lock:
            persisted = persist_maintenance(self.runtime_root, paused=True, reason="operator_pause_safe")
            self.is_paused = True
        return {
            "status": "PAUSED_SAFE",
            "message": "Autonomous command spawning paused safely. In-flight batches complete normally.",
            "timestamp": utc_now(),
            "maintenance_path": str(self.runtime_root / "maintenance-state.json"),
            "persisted_at": persisted["updated_at"],
        }

    def resume(self) -> Dict[str, Any]:
        with self._pause_lock:
            persist_maintenance(self.runtime_root, paused=False, reason="explicit_operator_resume")
            self.is_paused = False
        self._start_probe_thread()
        self.recover_unfinished()
        return {
            "status": "RESUMED",
            "message": "Autonomous recovery and execution resumed.",
            "timestamp": utc_now(),
        }

    def quiesce(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        """Persist maintenance, reject new work, and bounded-wait for owned workers."""
        self.pause_safe()
        def active(proc: Any) -> bool:
            tree_active = getattr(proc, "tree_active", None)
            return bool(tree_active()) if callable(tree_active) else proc.poll() is None
        deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), 60.0))
        while time.monotonic() < deadline:
            live = [p for p in self._worker_processes.values() if active(p)]
            if not live:
                break
            time.sleep(0.05)
        live_pids = [p.pid for p in self._worker_processes.values() if active(p)]
        return {
            "status": "QUIESCED" if not live_pids else "QUIESCE_TIMEOUT",
            "maintenance_state": PAUSED_SAFE,
            "in_flight_policy": "BOUNDED_WAIT_NO_NEW_CHILDREN",
            "in_flight_worker_pids": live_pids,
            "timestamp": utc_now(),
        }

    def request_shutdown(self, timeout_seconds: float = 10.0) -> Dict[str, Any]:
        result = self.quiesce(timeout_seconds)
        control_path = self.runtime_root.parent / "supervisor" / "control-request.json"
        atomic_json(control_path, {
            "contract_version": CONTRACT_VERSION,
            "action": "SHUTDOWN",
            "requested_by": "runtime_authenticated_api",
            "requested_at": utc_now(),
        })
        result["shutdown_requested"] = True
        result["control_path"] = str(control_path)
        return result

    def restart_worker(self, command_id: str) -> Dict[str, Any]:
        with self._pause_lock:
            state = self.store.read_state(command_id)
            if not state:
                raise ValueError(f"Command not found: {command_id}")
            old_pid = state.get("worker_pid")
            if self.is_paused:
                return {
                    "status": "PAUSED_SAFE",
                    "command_id": command_id,
                    "old_worker_pid": old_pid,
                    "new_worker_pid": None,
                    "worker_restarted": False,
                    "timestamp": utc_now(),
                }
            if pid_alive(old_pid):
                terminate_process_tree(old_pid)
            new_pid = self._spawn_worker(command_id, recovered=True)
        return {
            "status": "WORKER_RESTARTED",
            "command_id": command_id,
            "old_worker_pid": old_pid,
            "new_worker_pid": new_pid,
            "timestamp": utc_now(),
        }

    def trigger_relay(self, *, is_checkpoint: bool = False, force_remote: bool = False) -> Dict[str, Any]:
        h = self.health()
        reason = "OPERATOR_COMMAND_MANUAL_CHECKPOINT" if is_checkpoint else None
        queued = self.relay_worker.submit(
            maintenance=self.is_paused,
            runtime_health_dict=h,
            supervisor_pid=h.get("runtime_supervisor_pid"),
            major_gate_reason=reason,
            force_checkpoint=is_checkpoint,
        )
        return {
            "status": "RELAY_QUEUED" if queued else ("PAUSED_SAFE" if self.is_paused else "RELAY_BUSY"),
            "queued": queued,
            "relay_state": self.relay_worker.state(),
            "timestamp": utc_now(),
            "is_checkpoint": is_checkpoint,
            "force_remote_requested": force_remote,
        }

    def shutdown(self) -> None:
        self.stop_event.set()
        self.recovery_thread.join(timeout=3.0)
        if self.probe_thread is not None:
            self.probe_thread.join(timeout=3.0)
        self.telemetry_thread.join(timeout=3.0)
        self.relay_worker.close()
        for proc in list(self._worker_processes.values()):
            close = getattr(proc, "close", None)
            if callable(close):
                close()


class RuntimeHandler(BaseHTTPRequestHandler):
    server_version = "AOSRuntimeV1/1.0"

    @property
    def engine(self) -> RuntimeEngine:
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.runtime_token  # type: ignore[attr-defined]

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        return self.headers.get("X-AOS-Runtime-Token") == self.token

    def do_OPTIONS(self) -> None:
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "CORS_DISABLED"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/health":
            self._json(HTTPStatus.OK, self.engine.health())
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_RUNTIME_TOKEN"})
            return
        if parsed.path == "/v1/status":
            self._json(HTTPStatus.OK, self.engine.status())
            return
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["v1", "commands"]:
            command_id = parts[2]
            snap = self.engine.store.command_snapshot(command_id)
            if not snap["command"]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "COMMAND_NOT_FOUND"})
                return
            self._json(HTTPStatus.OK, snap)
            return
        if len(parts) == 4 and parts[:2] == ["v1", "commands"] and parts[3] == "events":
            command_id = parts[2]
            qs = parse_qs(parsed.query)
            try:
                after = int((qs.get("after_seq") or ["0"])[0])
            except ValueError:
                after = 0
            events = self.engine.store.read_events(command_id, after_seq=max(0, after))
            self._json(HTTPStatus.OK, {
                "contract_version": CONTRACT_VERSION,
                "command_id": command_id,
                "events": events,
            })
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        allowed_paths = (
            "/v1/commands/continue",
            "/v1/commands/pause-safe",
            "/v1/commands/resume",
            "/v1/commands/restart-worker",
            "/v1/commands/heartbeat-now",
            "/v1/commands/checkpoint-now",
            "/v1/commands/publish-relay-now",
            "/v1/commands/quiesce",
            "/v1/commands/shutdown",
        )
        if parsed.path not in allowed_paths:
            self._json(HTTPStatus.NOT_FOUND, {"error": "NOT_FOUND"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "INVALID_RUNTIME_TOKEN"})
            return
        if self.headers.get_content_type() != "application/json" and parsed.path == "/v1/commands/continue":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "BODY_SIZE_INVALID"})
            return
        payload = {}
        if length > 0:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be an object")
            except Exception as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "INVALID_JSON", "message": str(exc)[:500]})
                return

        try:
            if parsed.path == "/v1/commands/continue":
                result = self.engine.submit_continue(payload)
                self._json(HTTPStatus.ACCEPTED, result)
                return
            if parsed.path == "/v1/commands/pause-safe":
                result = self.engine.pause_safe()
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/resume":
                result = self.engine.resume()
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/quiesce":
                result = self.engine.quiesce(float(payload.get("timeout_seconds", 10.0)))
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/shutdown":
                result = self.engine.request_shutdown(float(payload.get("timeout_seconds", 10.0)))
                self._json(HTTPStatus.OK, result)
                threading.Thread(target=self.server.shutdown, name="aos-runtime-shutdown", daemon=True).start()
                return
            if parsed.path == "/v1/commands/restart-worker":
                cid = payload.get("command_id")
                if not cid:
                    raise ValueError("command_id is required")
                result = self.engine.restart_worker(str(cid))
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/heartbeat-now":
                result = self.engine.trigger_relay(is_checkpoint=False, force_remote=False)
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/checkpoint-now":
                result = self.engine.trigger_relay(is_checkpoint=True, force_remote=False)
                self._json(HTTPStatus.OK, result)
                return
            if parsed.path == "/v1/commands/publish-relay-now":
                result = self.engine.trigger_relay(is_checkpoint=True, force_remote=True)
                self._json(HTTPStatus.OK, result)
                return
        except Exception as exc:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": exc.__class__.__name__,
                "message": str(exc)[:1000],
            })

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _load_runtime_token(config: Dict[str, Any]) -> str:
    token_path = Path(config["runtime_token_path"]).expanduser().resolve()
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("Runtime token is missing or too short")
    return token


def serve(config_path: Path) -> int:
    config = load_config(config_path)
    env_slot = os.environ.get("AOS_RUNTIME_SLOT_ID")
    env_sha = os.environ.get("AOS_RUNTIME_SOURCE_SHA")
    if env_slot and config.get("runtime_slot_id") != env_slot:
        raise ValueError("Runtime launch slot identity does not match config")
    if env_sha and config.get("candidate_source_sha") != env_sha:
        raise ValueError("Runtime launch source SHA does not match config")
    env_nonce = os.environ.get("AOS_RUNTIME_LAUNCH_NONCE")
    env_supervisor = os.environ.get("AOS_RUNTIME_SUPERVISOR_PID")
    if env_nonce or env_slot or env_sha:
        if not env_nonce or not env_supervisor:
            raise ValueError("Runtime supervisor launch identity is incomplete")
        try:
            if int(env_supervisor) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Runtime supervisor PID is invalid")
    engine = RuntimeEngine(config)
    token = _load_runtime_token(config)
    server = ThreadingHTTPServer(("127.0.0.1", int(config["port"])), RuntimeHandler)
    server.daemon_threads = True
    server.engine = engine  # type: ignore[attr-defined]
    server.runtime_token = token  # type: ignore[attr-defined]
    runtime_root = Path(config["runtime_root"])
    atomic_json(runtime_root / "server.pid.json", {
        "pid": os.getpid(),
        "started_at": utc_now(),
        "contract_version": CONTRACT_VERSION,
        "runtime_source_sha": config.get("candidate_source_sha"),
        "runtime_slot_id": config.get("runtime_slot_id"),
        "runtime_launch_nonce": os.environ.get("AOS_RUNTIME_LAUNCH_NONCE"),
        "runtime_supervisor_pid": os.environ.get("AOS_RUNTIME_SUPERVISOR_PID"),
    })
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        engine.shutdown()
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Runtime V1 loopback API")
    parser.add_argument("--config", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return serve(Path(args.config).expanduser().resolve())
    except KeyboardInterrupt:
        return 130
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
