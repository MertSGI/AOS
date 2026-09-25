"""Cline Agentic Execution Backend for AOS.

Provides a first-class execution harness interface for official Cline CLI,
subordinate to AOS resource, policy, lineage, and verification authority.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from aos.agentic_resume import evaluate_agentic_resume
from aos.context_pack import handoff_seed
from aos.process_utils import popen_headless, run_headless
from aos.workspace_fingerprint import WorkspaceFingerprintError, compute_workspace_fingerprint
from aos.workers.cline_cli_probe import (
    CLINE_ADAPTER_CONTRACT_VERSION,
    build_cline_child_environment,
    get_cline_capability_store_path,
    resolve_cline_capability_status,
    resolve_cline_executable_identity,
)
from extensions.autonomy_fabric.execution_backend import (
    AgenticExecutionBackend,
    AgenticSessionIdentity,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)


_CONV_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")
_ALLOWED_USAGE = {
    "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalCost",
}


@dataclass(frozen=True)
class ClineJsonOutcome:
    valid: bool
    session_id: Optional[str]
    finish_reason: Optional[str]
    usage: Dict[str, Any] = field(default_factory=dict)
    failure_class: Optional[str] = None
    event_count: int = 0
    raw_error: Optional[str] = None


def parse_cline_stream_output(stdout: str, stderr: str, *, returncode: int) -> ClineJsonOutcome:
    """Parse Cline JSON/NDJSON output deterministically."""
    session_id: Optional[str] = None
    finish_reason: Optional[str] = None
    usage: Dict[str, Any] = {}
    failure_class: Optional[str] = None
    raw_error: Optional[str] = None
    count = 0

    all_text = f"{stdout}\n{stderr}"
    for line in all_text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        # Extract taskId or session ID
        candidate_task = event.get("taskId") or event.get("sessionId") or event.get("id")
        if candidate_task and isinstance(candidate_task, str) and not session_id:
            session_id = candidate_task

        # Hook / agent events
        event_type = event.get("type")
        if event_type == "run_result":
            finish_reason = event.get("finishReason")
            raw_usage = event.get("usage") or {}
            if isinstance(raw_usage, dict):
                usage = {k: v for k, v in raw_usage.items() if k in _ALLOWED_USAGE}

        if event_type == "error":
            raw_error = event.get("message") or str(event.get("error", ""))
            err_str = (raw_error or "").lower()
            if any(t in err_str for t in ("unauthorized", "re-authenticate", "auth", "login")):
                failure_class = "AUTH_UNAVAILABLE"
            elif any(t in err_str for t in ("cannot connect", "connectionrefused", "econnrefused", "timeout")):
                failure_class = "PROVIDER_UNREACHABLE"
            elif any(t in err_str for t in ("rate limit", "quota", "429")):
                failure_class = "QUOTA_EXHAUSTED"
            else:
                failure_class = "CONTRACT_FAILURE"

    valid = bool(returncode == 0 and finish_reason in {"complete", "success"} and not failure_class)
    if not valid and failure_class is None:
        if returncode != 0:
            failure_class = "EXECUTION_NONZERO_EXIT"
        else:
            failure_class = "INCOMPLETE_OUTCOME"

    return ClineJsonOutcome(
        valid=valid,
        session_id=session_id,
        finish_reason=finish_reason,
        usage=usage,
        failure_class=failure_class,
        event_count=count,
        raw_error=raw_error,
    )


def build_cline_argv(
    executable: str,
    workspace: str,
    *,
    data_dir: str,
    config_dir: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    session_id: Optional[str] = None,
    prompt: str = "",
) -> List[str]:
    """Construct safe, contained, headless argv for Cline."""
    argv = [
        executable,
        "--json",
        "--auto-approve", "true",
        "-c", workspace,
        "--data-dir", data_dir,
        "--config", config_dir,
    ]
    if provider:
        argv.extend(["-P", provider])
    if model:
        argv.extend(["-m", model])
    if api_key:
        argv.extend(["-k", api_key])
    if session_id:
        argv.extend(["--id", session_id])
    if prompt:
        # Prompt must be passed as quoted argument
        quoted_prompt = f'"{prompt.strip()}"' if not (prompt.startswith('"') and prompt.endswith('"')) else prompt
        argv.append(quoted_prompt)
    return argv


class ClineAgenticExecutionBackend(AgenticExecutionBackend):
    """AOS first-class agentic execution backend powered by official Cline CLI."""

    backend_id = "cline"
    resource_id = "cline_harness"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    @property
    def cost(self) -> ExecutionCost:
        if self.underlying_cost_class == "SUBSCRIPTION_INCLUDED":
            return ExecutionCost.SUBSCRIPTION_INCLUDED
        if self.underlying_cost_class == "PAID_CLOUD":
            return ExecutionCost.PAID_CLOUD
        if self.underlying_cost_class == "FREE_TIER_CLOUD":
            return ExecutionCost.FREE_TIER_CLOUD
        if self.underlying_cost_class == "QUOTA_LIMITED":
            return ExecutionCost.QUOTA_LIMITED
        return ExecutionCost.FREE_LOCAL
    supported_capabilities: Set[ExecutionCapability] = {
        ExecutionCapability.FILE_READ,
        ExecutionCapability.FILE_WRITE,
        ExecutionCapability.PATCH_APPLY,
        ExecutionCapability.PROCESS_EXEC,
        ExecutionCapability.GIT_READ,
        ExecutionCapability.TEST_EXECUTION,
        ExecutionCapability.LONG_HORIZON_AGENTIC_WORK,
    }

    def __init__(
        self,
        *,
        runner: Optional[
            Callable[[List[str], str, str, int, Dict[str, str]], subprocess.CompletedProcess]
        ] = None,
        capability_status_provider: Optional[Callable[[], str]] = None,
        executable_identity: Optional[Dict[str, str]] = None,
        capability_store_path: Optional[Path] = None,
        data_dir: Optional[str] = None,
        config_dir: Optional[str] = None,
        underlying_provider: str = "openai-compatible",
        underlying_model: str = "qwen-local",
        underlying_cost_class: str = "FREE_LOCAL",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._capability_status_provider = capability_status_provider
        self._injected_identity = dict(executable_identity or {}) or None
        self._store_path = capability_store_path
        self._clock = clock
        self._process_lock = threading.Lock()
        self._active_processes: Dict[str, Any] = {}

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        self._data_dir = data_dir or str(Path(local_app_data) / "AOS" / "sandbox" / "cline" / "data")
        self._config_dir = config_dir or str(Path(local_app_data) / "AOS" / "sandbox" / "cline" / "config")
        self.underlying_provider = underlying_provider
        self.underlying_model = underlying_model
        self.underlying_cost_class = underlying_cost_class

        os.makedirs(self._data_dir, exist_ok=True)
        os.makedirs(self._config_dir, exist_ok=True)

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _identity(self) -> Optional[Dict[str, str]]:
        return self._injected_identity or resolve_cline_executable_identity()

    def _capability_status(self) -> str:
        if self._capability_status_provider is not None:
            return str(self._capability_status_provider())
        return resolve_cline_capability_status(
            identity=self._identity(), capability_store=self._store_path
        )

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        status = self._capability_status()
        if status not in {"OPERATIONAL", "OPERATIONAL_BOUNDED", "TEST_DOUBLE"}:
            return ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.AUTH_UNAVAILABLE,
                self._now_iso(),
                source="CLINE_CAPABILITY_ATTESTATION",
                evidence={"status": status, "api_key_fallback": "DISABLED"},
            )
        return ExecutionAvailabilitySnapshot(
            ExecutionAvailabilityState.AVAILABLE,
            self._now_iso(),
            source="CLINE_CAPABILITY_ATTESTATION",
            evidence={
                "status": status,
                "provider": self.underlying_provider,
                "model": self.underlying_model,
                "cost_class": self.underlying_cost_class,
                "paid_fallback": "DISABLED",
            },
        )

    def get_health(self) -> ExecutionHealth:
        state = self.get_availability().state
        if state in {ExecutionAvailabilityState.AVAILABLE, ExecutionAvailabilityState.LOW_OR_SCARCE}:
            return ExecutionHealth.HEALTHY
        if state == ExecutionAvailabilityState.QUOTA_EXHAUSTED:
            return ExecutionHealth.QUOTA_EXHAUSTED
        return ExecutionHealth.UNAVAILABLE

    @staticmethod
    def _source_sha(request: ExecutionRequest, prior: Optional[AgenticSessionIdentity]) -> str:
        value = str(request.payload.get("source_sha") or (prior.source_sha if prior else ""))
        if len(value) != 40 or any(ch not in "0123456789abcdefABCDEF" for ch in value):
            raise ValueError("canonical source SHA required")
        return value.lower()

    @staticmethod
    def _prompt(request: ExecutionRequest, context_pack: Dict[str, Any]) -> str:
        value = request.payload.get("prompt")
        prompt = value if isinstance(value, str) and value.strip() else json.dumps(
            context_pack, ensure_ascii=False, sort_keys=True
        )
        if not prompt or len(prompt) > 64_000:
            raise ValueError("Cline prompt/context must be bounded and non-empty")
        return prompt

    def _default_runner(
        self, argv: List[str], cwd: str, prompt: str, timeout: int, env: Dict[str, str], request_id: str
    ) -> subprocess.CompletedProcess:
        # Run headless with process tree containment
        proc = popen_headless(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._process_lock:
            self._active_processes[request_id] = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, int(proc.returncode or 0), stdout, stderr)
        except subprocess.TimeoutExpired:
            proc.terminate_tree()
            stdout, stderr = proc.communicate()
            return subprocess.CompletedProcess(argv, 124, stdout, stderr)
        finally:
            with self._process_lock:
                self._active_processes.pop(request_id, None)
            if proc.poll() is not None:
                proc.wait()

    @staticmethod
    def _changed_paths(workspace: str) -> List[str]:
        commands = (
            ["git", "-C", workspace, "diff", "--name-only", "-z", "HEAD"],
            ["git", "-C", workspace, "ls-files", "--others", "--exclude-standard", "-z"],
        )
        raw = b""
        for command in commands:
            result = run_headless(command, timeout=30, text=False)
            if result.returncode:
                raise WorkspaceFingerprintError("unable to inspect Cline workspace mutations")
            raw += result.stdout
        return sorted({os.fsdecode(item).replace("\\", "/") for item in raw.split(b"\0") if item})

    @staticmethod
    def _in_scope(path: str, scopes: List[str]) -> bool:
        if not scopes:
            return True
        value = path.strip("/").lower()
        return any(
            value == scope.strip("/").lower()
            or value.startswith(scope.strip("/").lower() + "/")
            for scope in scopes if scope.strip("/")
        )

    @staticmethod
    def _artifact_hashes(workspace: str, paths: List[str]) -> Dict[str, str]:
        root = Path(workspace).resolve()
        values: Dict[str, str] = {}
        for relative in paths:
            path = (root / relative).resolve()
            try:
                if os.path.commonpath((str(root), str(path))) != str(root) or not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                values[relative] = digest.hexdigest()
            except OSError:
                continue
        return values

    @staticmethod
    def _work_signature(request: ExecutionRequest) -> str:
        return hashlib.sha256(json.dumps({
            "task_id": request.task_id,
            "project_id": request.project_id,
            "operation_class": request.operation_class,
            "write_scope": sorted(request.write_scope),
            "expected_changed_paths": sorted(request.expected_changed_paths),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _failure(
        self,
        request: ExecutionRequest,
        failure_class: str,
        availability: ExecutionAvailabilitySnapshot,
        *,
        status: str = "DEGRADED",
    ) -> ExecutionResult:
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="cline_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status=status,
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=[failure_class],
            evidence_payload={"failure_class": failure_class},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            availability=availability,
        )

    def start(self, request: ExecutionRequest, context_pack: Dict[str, Any]) -> ExecutionResult:
        return self._run(request, context_pack, None)

    def resume(
        self, request: ExecutionRequest, identity: AgenticSessionIdentity, context_pack: Dict[str, Any]
    ) -> ExecutionResult:
        return self._run(request, context_pack, identity)

    def _run(
        self,
        request: ExecutionRequest,
        context_pack: Dict[str, Any],
        prior: Optional[AgenticSessionIdentity],
    ) -> ExecutionResult:
        availability = self.get_availability()
        if availability.state not in {
            ExecutionAvailabilityState.AVAILABLE,
            ExecutionAvailabilityState.LOW_OR_SCARCE,
        }:
            return self._failure(request, f"CLINE_{availability.state.value}", availability)
        executable = self._identity()
        if executable is None:
            return self._failure(request, "CLINE_EXECUTABLE_UNAVAILABLE", availability)
        try:
            source_sha = self._source_sha(request, prior)
            before = compute_workspace_fingerprint(request.workspace, source_sha=source_sha)
            prompt = self._prompt(request, context_pack)
        except (ValueError, OSError, WorkspaceFingerprintError):
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now_iso(), source="CLINE_PRELAUNCH_GATE",
                evidence={"reason": "PRELAUNCH_CONTRACT_FAILURE"},
            )
            return self._failure(request, "CLINE_PRELAUNCH_CONTRACT_FAILURE", failed)

        if prior is not None:
            if request.task_id in prior.completed_work_unit_ids:
                return self._failure(
                    request, "COMPLETED_WORK_MUST_NOT_BE_DUPLICATED", availability,
                )
            if not prior.session_or_thread_id:
                return self._failure(request, "STALE_AGENT_SESSION:MISSING_SESSION_ID", availability)
            compatibility = evaluate_agentic_resume(
                prior,
                backend_id=self.backend_id,
                resource_id=self.resource_id,
                source_sha=source_sha,
                workspace_fingerprint=before.sha256,
                adapter_contract_version=CLINE_ADAPTER_CONTRACT_VERSION,
                executable_sha256=executable.get("sha256"),
                auth_mode="provider_bounded",
                objective_terminal=bool(request.payload.get("objective_terminal", False)),
            )
            if not compatibility.compatible:
                return self._failure(
                    request, f"STALE_AGENT_SESSION:{compatibility.reason.value}", availability
                )

        try:
            argv = build_cline_argv(
                executable["path"],
                request.workspace,
                data_dir=self._data_dir,
                config_dir=self._config_dir,
                provider=self.underlying_provider,
                model=self.underlying_model,
                session_id=(prior.session_or_thread_id if prior else None),
                prompt=prompt,
            )
            env = build_cline_child_environment()
            if self._runner is not None:
                completed = self._runner(
                    argv, request.workspace, prompt, request.timeout_seconds, env
                )
            else:
                completed = self._default_runner(
                    argv, request.workspace, prompt, request.timeout_seconds, env, request.request_id
                )
            outcome = parse_cline_stream_output(
                str(completed.stdout or ""), str(completed.stderr or ""), returncode=completed.returncode
            )
        except Exception:
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
                self._now_iso(), source="CLINE_EXECUTION",
                evidence={"reason": "BOUNDED_EXECUTION_EXCEPTION"},
            )
            return self._failure(request, "CLINE_TEMPORARILY_UNAVAILABLE", failed)

        if not outcome.valid:
            state = {
                "QUOTA_EXHAUSTED": ExecutionAvailabilityState.QUOTA_EXHAUSTED,
                "AUTH_UNAVAILABLE": ExecutionAvailabilityState.AUTH_UNAVAILABLE,
                "PROVIDER_UNREACHABLE": ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
            }.get(outcome.failure_class, ExecutionAvailabilityState.CONTRACT_FAILURE)
            failed = ExecutionAvailabilitySnapshot(
                state, self._now_iso(),
                source="CLINE_STREAM_TERMINAL",
                evidence={"finish_reason": outcome.finish_reason or "MISSING", "error": outcome.raw_error},
            )
            return self._failure(request, f"CLINE_{outcome.failure_class}", failed)

        try:
            changed = self._changed_paths(request.workspace)
            scopes = list(request.write_scope or request.expected_changed_paths)
            if any(not self._in_scope(path, scopes) for path in changed):
                return self._failure(
                    request, "CLINE_WRITE_SCOPE_VIOLATION", availability, status="FAILED"
                )
            artifacts = self._artifact_hashes(
                request.workspace, sorted(set(changed) | set(request.expected_artifacts))
            )
            after = compute_workspace_fingerprint(request.workspace, source_sha=source_sha)
        except (OSError, WorkspaceFingerprintError):
            return self._failure(
                request, "CLINE_POST_EXECUTION_VERIFICATION_FAILED", availability, status="FAILED"
            )

        seed = handoff_seed(context_pack) if prior is None else {}
        ids = sorted(set((prior.completed_work_unit_ids if prior else seed.get("completed_work_unit_ids", [])) + [request.task_id]))
        signatures = dict(prior.completed_work_unit_signatures if prior else seed.get("completed_work_unit_signatures", {}))
        signatures[request.task_id] = self._work_signature(request)
        all_artifacts = dict(prior.artifact_hashes if prior else seed.get("artifact_hashes", {}))
        all_artifacts.update(artifacts)
        last_artifact = prior.last_successful_artifact if prior else None
        if artifacts:
            path = sorted(artifacts)[-1]
            last_artifact = {"path": path, "sha256": artifacts[path]}

        identity = AgenticSessionIdentity(
            resource_id=self.resource_id,
            backend_id=self.backend_id,
            session_or_thread_id=outcome.session_id or f"cline-{request.task_id}",
            workspace_fingerprint=after.sha256,
            source_sha=source_sha,
            checkpoint_id=str(request.payload.get("checkpoint_id") or request.request_id),
            last_successful_turn=(prior.last_successful_turn + 1 if prior else 1),
            last_successful_artifact=last_artifact,
            started_at=prior.started_at if prior else self._now_iso(),
            updated_at=self._now_iso(),
            adapter_contract_version=CLINE_ADAPTER_CONTRACT_VERSION,
            backend_version=executable.get("version"),
            executable_sha256=executable.get("sha256"),
            auth_mode="provider_bounded",
            objective_id=str(request.payload.get("objective_id") or request.task_id),
            last_terminal_event=outcome.finish_reason or "complete",
            completed_work_unit_ids=ids,
            completed_work_unit_signatures=signatures,
            artifact_hashes=all_artifacts,
            superseded_session_ids=list(prior.superseded_session_ids if prior else seed.get("superseded_session_ids", [])),
        )
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="cline_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            changed_paths=changed,
            artifact_hashes=artifacts,
            stdout_digest="Cline completed a verified structured turn",
            resource_usage=outcome.usage,
            evidence_payload={
                "finish_reason": outcome.finish_reason,
                "event_count": outcome.event_count,
                "resume_mode": "SESSION_ID" if prior else "NEW_SESSION",
                "underlying_provider": self.underlying_provider,
                "underlying_model": self.underlying_model,
                "cost_class": self.underlying_cost_class,
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            agentic_identity=identity,
            availability=availability,
        )

    def interrupt(self, execution_id: str) -> None:
        with self._process_lock:
            process = self._active_processes.get(execution_id)
        if process is not None:
            process.terminate_tree()
