"""Codex CLI ChatGPT-subscription agentic execution backend."""
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
from aos.workers.codex_cli_probe import (
    CODEX_ADAPTER_CONTRACT_VERSION,
    build_codex_child_environment,
    get_codex_capability_store_path,
    resolve_codex_capability_status,
    resolve_codex_executable_identity,
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


_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ALLOWED_EVENTS = {
    "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
    "item.started", "item.updated", "item.completed",
}
_ALLOWED_USAGE = {
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
}


@dataclass(frozen=True)
class CodexJsonlOutcome:
    valid: bool
    thread_id: Optional[str]
    terminal_event: Optional[str]
    usage: Dict[str, int] = field(default_factory=dict)
    failure_class: Optional[str] = None
    event_count: int = 0


def _structured_failure_class(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True).upper()
    if any(token in text for token in ("RATE_LIMIT", "QUOTA", "RESOURCE_EXHAUSTED", "429")):
        return "QUOTA_EXHAUSTED"
    if any(token in text for token in ("AUTH", "LOGIN", "UNAUTHORIZED", "FORBIDDEN", "401", "403")):
        return "AUTH_UNAVAILABLE"
    if any(token in text for token in ("TIMEOUT", "NETWORK", "CONNECTION", "CAPACITY", "UNAVAILABLE")):
        return "TEMPORARILY_UNAVAILABLE"
    return "CONTRACT_FAILURE"


def parse_codex_exec_jsonl(stdout: str, *, returncode: int) -> CodexJsonlOutcome:
    thread_id: Optional[str] = None
    terminal: Optional[str] = None
    usage: Dict[str, int] = {}
    failure_class: Optional[str] = None
    count = 0
    malformed = False
    thread_started_count = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        count += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        event_type = event.get("type")
        if not isinstance(event_type, str):
            malformed = True
            continue
        if event_type not in _ALLOWED_EVENTS:
            malformed = True
            continue
        if event_type == "thread.started":
            thread_started_count += 1
            candidate = event.get("thread_id")
            if not isinstance(candidate, str) or not _UUID.fullmatch(candidate):
                malformed = True
            elif thread_id is not None and thread_id != candidate:
                malformed = True
            else:
                thread_id = candidate.lower()
        if event_type in {"turn.completed", "turn.failed", "error"}:
            if terminal is not None:
                malformed = True
            terminal = event_type
            if event_type != "turn.completed":
                failure_class = _structured_failure_class(event)
        if event_type == "turn.completed":
            raw_usage = event.get("usage", {})
            if isinstance(raw_usage, dict):
                for key, value in raw_usage.items():
                    if key in _ALLOWED_USAGE and isinstance(value, int) and value >= 0:
                        usage[key] = value
    valid = bool(
        not malformed
        and returncode == 0
        and thread_id
        and thread_started_count == 1
        and terminal == "turn.completed"
    )
    if not valid and failure_class is None:
        failure_class = "CONTRACT_FAILURE"
    return CodexJsonlOutcome(
        valid=valid,
        thread_id=thread_id,
        terminal_event=terminal,
        usage=usage,
        failure_class=failure_class,
        event_count=count,
    )


def build_codex_exec_argv(
    executable: str,
    workspace: str,
    *,
    thread_id: Optional[str] = None,
) -> List[str]:
    argv = [
        executable,
        "--ask-for-approval", "never",
        "--sandbox", "workspace-write",
        "--cd", workspace,
        "exec",
        "--ignore-user-config",
        "--json",
    ]
    if thread_id is None:
        argv.append("-")
    else:
        if not _UUID.fullmatch(thread_id):
            raise ValueError("Codex resume requires an exact UUID")
        argv.extend(["resume", thread_id.lower(), "-"])
    if "--last" in argv or any("dangerously-bypass" in item for item in argv):
        raise ValueError("unsafe Codex invocation option")
    return argv


class CodexCliExecutionBackend(AgenticExecutionBackend):
    backend_id = "codex_cli"
    resource_id = "local_codex_cli_chatgpt_subscription"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED
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
        quota_snapshot_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        executable_identity: Optional[Dict[str, str]] = None,
        capability_store_path: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runner = runner
        self._capability_status_provider = capability_status_provider
        self._quota_snapshot_provider = quota_snapshot_provider
        self._injected_identity = dict(executable_identity or {}) or None
        self._store_path = capability_store_path
        self._clock = clock
        self._process_lock = threading.Lock()
        self._active_processes: Dict[str, Any] = {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _identity(self) -> Optional[Dict[str, str]]:
        return self._injected_identity or resolve_codex_executable_identity()

    def _capability_status(self) -> str:
        if self._capability_status_provider is not None:
            return str(self._capability_status_provider())
        return resolve_codex_capability_status(
            identity=self._identity(), store_path=self._store_path
        )

    def _quota_snapshot(self) -> Dict[str, Any]:
        if self._quota_snapshot_provider is not None:
            return dict(self._quota_snapshot_provider())
        path = self._store_path or get_codex_capability_store_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            quota = ((value.get("extensions") or {}).get("codex_cli") or {}).get("quota", {})
            return dict(quota) if isinstance(quota, dict) else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        capability = self._capability_status()
        if capability not in {"PROVEN", "TEST_DOUBLE"}:
            return ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.AUTH_UNAVAILABLE,
                self._now_iso(),
                source="CODEX_CAPABILITY_ATTESTATION",
                evidence={"auth_mode": "unavailable", "api_key_fallback": "DISABLED"},
            )
        quota = self._quota_snapshot()
        state_text = str(quota.get("state") or "UNKNOWN")
        try:
            state = ExecutionAvailabilityState(state_text)
        except ValueError:
            state = ExecutionAvailabilityState.UNKNOWN
        if capability == "TEST_DOUBLE" and not quota:
            state = ExecutionAvailabilityState.AVAILABLE
        return ExecutionAvailabilitySnapshot(
            state,
            str(quota.get("observed_at") or self._now_iso()),
            retry_after_epoch=(
                float(quota["retry_after_epoch"])
                if isinstance(quota.get("retry_after_epoch"), (int, float)) else None
            ),
            source=str(quota.get("source") or "CODEX_CAPABILITY_ATTESTATION"),
            evidence={
                "auth_mode": "chatgpt",
                "api_key_fallback": "DISABLED",
                **({"primary_used_percent": quota["primary_used_percent"]} if isinstance(quota.get("primary_used_percent"), (int, float)) else {}),
                **({"secondary_used_percent": quota["secondary_used_percent"]} if isinstance(quota.get("secondary_used_percent"), (int, float)) else {}),
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
            raise ValueError("Codex prompt/context must be bounded and non-empty")
        return prompt

    def _default_runner(
        self, argv: List[str], cwd: str, prompt: str, timeout: int, env: Dict[str, str], request_id: str
    ) -> subprocess.CompletedProcess:
        process = popen_headless(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._process_lock:
            self._active_processes[request_id] = process
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout)
            return subprocess.CompletedProcess(argv, int(process.returncode or 0), stdout, stderr)
        except subprocess.TimeoutExpired:
            process.terminate_tree()
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(argv, 124, stdout, stderr)
        finally:
            with self._process_lock:
                self._active_processes.pop(request_id, None)
            if process.poll() is not None:
                process.wait()

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
                raise WorkspaceFingerprintError("unable to inspect Codex workspace mutations")
            raw += result.stdout
        return sorted({os.fsdecode(item).replace("\\", "/") for item in raw.split(b"\0") if item})

    @staticmethod
    def _in_scope(path: str, scopes: List[str]) -> bool:
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
            worker_id="codex_cli",
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
            return self._failure(request, f"CODEX_{availability.state.value}", availability)
        executable = self._identity()
        if executable is None:
            return self._failure(request, "CODEX_EXECUTABLE_UNAVAILABLE", availability)
        try:
            source_sha = self._source_sha(request, prior)
            before = compute_workspace_fingerprint(request.workspace, source_sha=source_sha)
            prompt = self._prompt(request, context_pack)
        except (ValueError, OSError, WorkspaceFingerprintError):
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now_iso(), source="CODEX_PRELAUNCH_GATE",
                evidence={"reason": "PRELAUNCH_CONTRACT_FAILURE"},
            )
            return self._failure(request, "CODEX_PRELAUNCH_CONTRACT_FAILURE", failed)
        if prior is not None:
            if request.task_id in prior.completed_work_unit_ids:
                return self._failure(
                    request, "COMPLETED_WORK_MUST_NOT_BE_DUPLICATED", availability,
                )
            if not prior.session_or_thread_id or not _UUID.fullmatch(prior.session_or_thread_id):
                return self._failure(request, "STALE_AGENT_SESSION:MISSING_SESSION_ID", availability)
            compatibility = evaluate_agentic_resume(
                prior,
                backend_id=self.backend_id,
                resource_id=self.resource_id,
                source_sha=source_sha,
                workspace_fingerprint=before.sha256,
                adapter_contract_version=CODEX_ADAPTER_CONTRACT_VERSION,
                executable_sha256=executable["sha256"],
                auth_mode="chatgpt",
                objective_terminal=bool(request.payload.get("objective_terminal", False)),
            )
            if not compatibility.compatible:
                return self._failure(
                    request, f"STALE_AGENT_SESSION:{compatibility.reason.value}", availability
                )
        try:
            argv = build_codex_exec_argv(
                executable["path"], request.workspace,
                thread_id=(prior.session_or_thread_id if prior else None),
            )
            env = build_codex_child_environment()
            if self._runner is not None:
                completed = self._runner(
                    argv, request.workspace, prompt, request.timeout_seconds, env
                )
            else:
                completed = self._default_runner(
                    argv, request.workspace, prompt, request.timeout_seconds, env, request.request_id
                )
            outcome = parse_codex_exec_jsonl(
                str(completed.stdout or ""), returncode=completed.returncode
            )
            if completed.returncode == 124:
                outcome = CodexJsonlOutcome(
                    False, outcome.thread_id, outcome.terminal_event, outcome.usage,
                    "TEMPORARILY_UNAVAILABLE", outcome.event_count,
                )
        except Exception:
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
                self._now_iso(), source="CODEX_EXECUTION",
                evidence={"reason": "BOUNDED_EXECUTION_EXCEPTION"},
            )
            return self._failure(request, "CODEX_TEMPORARILY_UNAVAILABLE", failed)

        if prior is not None and outcome.thread_id and outcome.thread_id != prior.session_or_thread_id.lower():
            outcome = CodexJsonlOutcome(
                False, outcome.thread_id, outcome.terminal_event, outcome.usage,
                "CONTRACT_FAILURE", outcome.event_count,
            )
        if not outcome.valid:
            state = {
                "QUOTA_EXHAUSTED": ExecutionAvailabilityState.QUOTA_EXHAUSTED,
                "AUTH_UNAVAILABLE": ExecutionAvailabilityState.AUTH_UNAVAILABLE,
                "TEMPORARILY_UNAVAILABLE": ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
            }.get(outcome.failure_class, ExecutionAvailabilityState.CONTRACT_FAILURE)
            failed = ExecutionAvailabilitySnapshot(
                state, self._now_iso(),
                retry_after_epoch=availability.retry_after_epoch if state == ExecutionAvailabilityState.QUOTA_EXHAUSTED else None,
                source="CODEX_JSONL_TERMINAL",
                evidence={"terminal_event": outcome.terminal_event or "MISSING"},
            )
            return self._failure(request, f"CODEX_{outcome.failure_class}", failed)

        try:
            changed = self._changed_paths(request.workspace)
            scopes = list(request.write_scope or request.expected_changed_paths)
            if any(not self._in_scope(path, scopes) for path in changed):
                return self._failure(
                    request, "CODEX_WRITE_SCOPE_VIOLATION", availability, status="FAILED"
                )
            artifacts = self._artifact_hashes(
                request.workspace, sorted(set(changed) | set(request.expected_artifacts))
            )
            after = compute_workspace_fingerprint(request.workspace, source_sha=source_sha)
        except (OSError, WorkspaceFingerprintError):
            return self._failure(
                request, "CODEX_POST_EXECUTION_VERIFICATION_FAILED", availability, status="FAILED"
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
            session_or_thread_id=outcome.thread_id,
            workspace_fingerprint=after.sha256,
            source_sha=source_sha,
            checkpoint_id=str(request.payload.get("checkpoint_id") or request.request_id),
            last_successful_turn=(prior.last_successful_turn + 1 if prior else 1),
            last_successful_artifact=last_artifact,
            started_at=prior.started_at if prior else self._now_iso(),
            updated_at=self._now_iso(),
            adapter_contract_version=CODEX_ADAPTER_CONTRACT_VERSION,
            backend_version=executable["version"],
            executable_sha256=executable["sha256"],
            auth_mode="chatgpt",
            objective_id=str(request.payload.get("objective_id") or request.task_id),
            last_terminal_event="turn.completed",
            completed_work_unit_ids=ids,
            completed_work_unit_signatures=signatures,
            artifact_hashes=all_artifacts,
            superseded_session_ids=list(prior.superseded_session_ids if prior else seed.get("superseded_session_ids", [])),
        )
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="codex_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            changed_paths=changed,
            artifact_hashes=artifacts,
            stdout_digest="Codex CLI completed a verified structured turn",
            resource_usage=outcome.usage,
            evidence_payload={
                "terminal_event": "turn.completed",
                "event_count": outcome.event_count,
                "resume_mode": "EXACT_UUID" if prior else "NEW_THREAD",
                "auth_mode": "chatgpt",
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
