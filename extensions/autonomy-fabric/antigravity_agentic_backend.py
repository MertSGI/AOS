"""First-class Antigravity agentic backend using the existing proven adapters."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from aos.agentic_resume import evaluate_agentic_resume
from aos.context_pack import handoff_seed
from aos.process_utils import run_headless
from aos.workspace_fingerprint import (
    WorkspaceFingerprintError,
    compute_workspace_fingerprint,
)
from aos.workers.antigravity import (
    ADAPTER_CONTRACT_VERSION,
    resolve_capability_status,
    resolve_executable_identity,
)
from extensions.autonomy_fabric.antigravity_adapter import (
    AntigravityCLIAdapter,
    AntigravityStatus,
    BaseAntigravityAdapter,
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


class AntigravityAgenticExecutionBackend(AgenticExecutionBackend):
    backend_id = "antigravity"
    resource_id = "local_antigravity_subscription"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    cost = ExecutionCost.SUBSCRIPTION_INCLUDED
    supported_capabilities: Set[ExecutionCapability] = {
        ExecutionCapability.ANTIGRAVITY,
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
        adapter: Optional[BaseAntigravityAdapter] = None,
        *,
        capability_status_provider: Optional[Callable[[], str]] = None,
        executable_identity: Optional[Dict[str, str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._adapter = adapter
        self._clock = clock
        self._injected_identity = dict(executable_identity or {}) or None
        self._capability_status_provider = capability_status_provider
        self._active_execution_ids: Set[str] = set()

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _identity(self) -> Optional[Dict[str, str]]:
        return self._injected_identity or resolve_executable_identity("agy")

    def _capability_status(self) -> str:
        if self._capability_status_provider is not None:
            return str(self._capability_status_provider())
        identity = self._identity()
        return resolve_capability_status("agy", identity=identity)

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        capability = self._capability_status()
        identity = self._identity()
        if capability not in {"PROVEN", "TEST_DOUBLE"}:
            state = ExecutionAvailabilityState.CONTRACT_FAILURE
        elif identity is None and capability != "TEST_DOUBLE":
            state = ExecutionAvailabilityState.CONTRACT_FAILURE
        else:
            state = ExecutionAvailabilityState.AVAILABLE
        return ExecutionAvailabilitySnapshot(
            state=state,
            observed_at=self._now_iso(),
            source="MACHINE_LOCAL_CAPABILITY_ATTESTATION",
            evidence={
                "capability_status": capability,
                "adapter_contract_version": ADAPTER_CONTRACT_VERSION,
            },
        )

    def get_health(self) -> ExecutionHealth:
        state = self.get_availability().state
        if state in {
            ExecutionAvailabilityState.AVAILABLE,
            ExecutionAvailabilityState.LOW_OR_SCARCE,
        }:
            return ExecutionHealth.HEALTHY
        if state == ExecutionAvailabilityState.QUOTA_EXHAUSTED:
            return ExecutionHealth.QUOTA_EXHAUSTED
        return ExecutionHealth.UNAVAILABLE

    def _adapter_instance(self) -> BaseAntigravityAdapter:
        if self._adapter is not None:
            return self._adapter
        identity = self._identity()
        if identity is None:
            raise ValueError("ANTIGRAVITY_EXECUTABLE_IDENTITY_UNAVAILABLE")
        self._adapter = AntigravityCLIAdapter(cli_binary_path=identity["path"])
        return self._adapter

    @staticmethod
    def _source_sha(request: ExecutionRequest, identity: Optional[AgenticSessionIdentity]) -> str:
        source_sha = str(
            request.payload.get("source_sha")
            or (identity.source_sha if identity is not None else "")
        )
        if len(source_sha) < 40:
            raise ValueError("AGENTIC_SOURCE_SHA_REQUIRED")
        return source_sha

    @staticmethod
    def _prompt(request: ExecutionRequest, context_pack: Dict[str, Any]) -> str:
        prompt = request.payload.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            if len(prompt) > 64_000:
                raise ValueError("AGENTIC_PROMPT_TOO_LARGE")
            return prompt
        encoded = json.dumps(context_pack, ensure_ascii=False, sort_keys=True)
        if len(encoded) > 64_000:
            raise ValueError("CONTEXT_PACK_TOO_LARGE")
        return encoded

    @staticmethod
    def _changed_paths(workspace: str) -> List[str]:
        tracked = run_headless(
            ["git", "-C", workspace, "diff", "--name-only", "-z", "HEAD"],
            timeout=30,
            text=False,
            check=False,
        )
        untracked = run_headless(
            ["git", "-C", workspace, "ls-files", "--others", "--exclude-standard", "-z"],
            timeout=30,
            text=False,
            check=False,
        )
        if tracked.returncode or untracked.returncode:
            raise WorkspaceFingerprintError("unable to inspect Antigravity workspace mutations")
        paths = {
            os.fsdecode(raw).replace("\\", "/")
            for raw in (tracked.stdout + untracked.stdout).split(b"\0")
            if raw
        }
        return sorted(paths)

    @staticmethod
    def _in_scope(path: str, allowed: List[str]) -> bool:
        normalized = path.strip("/").lower()
        return any(
            normalized == item.strip("/").lower()
            or normalized.startswith(item.strip("/").lower() + "/")
            for item in allowed
            if item.strip("/")
        )

    @staticmethod
    def _artifact_hashes(workspace: str, paths: List[str]) -> Dict[str, str]:
        result: Dict[str, str] = {}
        root = Path(workspace).resolve()
        for relative in paths:
            target = (root / relative).resolve()
            try:
                if os.path.commonpath((str(root), str(target))) != str(root):
                    continue
                if target.is_file():
                    digest = hashlib.sha256()
                    with target.open("rb") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                    result[relative] = digest.hexdigest()
            except OSError:
                continue
        return result

    @staticmethod
    def _work_signature(request: ExecutionRequest) -> str:
        safe = {
            "task_id": request.task_id,
            "project_id": request.project_id,
            "operation_class": request.operation_class,
            "write_scope": sorted(request.write_scope),
            "expected_changed_paths": sorted(request.expected_changed_paths),
        }
        return hashlib.sha256(json.dumps(
            safe, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def _failure(
        self,
        request: ExecutionRequest,
        reason: str,
        availability: ExecutionAvailabilitySnapshot,
        *,
        status: str = "DEGRADED",
    ) -> ExecutionResult:
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="antigravity_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status=status,
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=[reason],
            availability=availability,
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )

    def start(
        self, request: ExecutionRequest, context_pack: Dict[str, Any]
    ) -> ExecutionResult:
        return self._run(request, context_pack, None)

    def resume(
        self,
        request: ExecutionRequest,
        identity: AgenticSessionIdentity,
        context_pack: Dict[str, Any],
    ) -> ExecutionResult:
        return self._run(request, context_pack, identity)

    def _run(
        self,
        request: ExecutionRequest,
        context_pack: Dict[str, Any],
        prior: Optional[AgenticSessionIdentity],
    ) -> ExecutionResult:
        availability = self.get_availability()
        if availability.state != ExecutionAvailabilityState.AVAILABLE:
            return self._failure(request, "ANTIGRAVITY_UNAVAILABLE", availability)
        try:
            source_sha = self._source_sha(request, prior)
            before = compute_workspace_fingerprint(
                request.workspace, source_sha=source_sha
            )
        except (ValueError, WorkspaceFingerprintError, OSError):
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now_iso(),
                source="WORKSPACE_FINGERPRINT",
                evidence={"reason": "WORKSPACE_FINGERPRINT_UNAVAILABLE"},
            )
            return self._failure(request, "WORKSPACE_FINGERPRINT_UNAVAILABLE", failed)

        executable = self._identity() or {}
        if prior is not None:
            compatibility = evaluate_agentic_resume(
                prior,
                backend_id=self.backend_id,
                resource_id=self.resource_id,
                source_sha=source_sha,
                workspace_fingerprint=before.sha256,
                adapter_contract_version=ADAPTER_CONTRACT_VERSION,
                executable_sha256=executable.get("sha256"),
                auth_mode="subscription",
                objective_terminal=bool(request.payload.get("objective_terminal", False)),
            )
            if not compatibility.compatible:
                return self._failure(
                    request,
                    f"STALE_AGENT_SESSION:{compatibility.reason.value}",
                    availability,
                )

        try:
            adapter = self._adapter_instance()
            self._active_execution_ids.add(request.request_id)
            response = adapter.execute_prompt(
                self._prompt(request, context_pack),
                conversation_id=(prior.session_or_thread_id if prior else None),
                workspace_path=request.workspace,
                output_format="stream-json",
                continue_conversation=prior is not None,
            )
        except Exception:
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
                self._now_iso(),
                source="ANTIGRAVITY_EXECUTION",
                evidence={"reason": "BOUNDED_EXECUTION_EXCEPTION"},
            )
            return self._failure(request, "ANTIGRAVITY_EXECUTION_UNAVAILABLE", failed)
        finally:
            self._active_execution_ids.discard(request.request_id)

        error_upper = str(response.error_message or "").upper()
        if any(token in error_upper for token in ("QUOTA", "RESOURCEEXHAUSTED", "429")):
            exhausted = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.QUOTA_EXHAUSTED,
                self._now_iso(),
                source="ANTIGRAVITY_STRUCTURED_RESULT",
                evidence={"reason": "QUOTA_EXHAUSTED"},
            )
            return self._failure(request, "ANTIGRAVITY_QUOTA_EXHAUSTED", exhausted)
        if (
            response.status != AntigravityStatus.SUCCESS
            or not response.conversation_id
        ):
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now_iso(),
                source="ANTIGRAVITY_STRUCTURED_RESULT",
                evidence={"terminal_status": response.status.value},
            )
            return self._failure(request, "ANTIGRAVITY_TERMINAL_CONTRACT_FAILURE", failed)

        try:
            changed_paths = self._changed_paths(request.workspace)
            allowed = list(request.write_scope or request.expected_changed_paths)
            unexpected = [path for path in changed_paths if not self._in_scope(path, allowed)]
            if unexpected:
                return self._failure(
                    request,
                    "ANTIGRAVITY_WRITE_SCOPE_VIOLATION",
                    availability,
                    status="FAILED",
                )
            artifacts = self._artifact_hashes(
                request.workspace,
                sorted(set(changed_paths) | set(request.expected_artifacts)),
            )
            after = compute_workspace_fingerprint(
                request.workspace, source_sha=source_sha
            )
        except (OSError, WorkspaceFingerprintError):
            return self._failure(
                request, "ANTIGRAVITY_POST_EXECUTION_VERIFICATION_FAILED", availability,
                status="FAILED",
            )

        seed = handoff_seed(context_pack) if prior is None else {}
        completed_ids = sorted(set(
            (prior.completed_work_unit_ids if prior else seed.get("completed_work_unit_ids", [])) + [request.task_id]
        ))
        signatures = dict(prior.completed_work_unit_signatures if prior else seed.get("completed_work_unit_signatures", {}))
        signatures[request.task_id] = self._work_signature(request)
        all_artifacts = dict(prior.artifact_hashes if prior else seed.get("artifact_hashes", {}))
        all_artifacts.update(artifacts)
        last_artifact = None
        if artifacts:
            path = sorted(artifacts)[-1]
            last_artifact = {"path": path, "sha256": artifacts[path]}
        identity = AgenticSessionIdentity(
            resource_id=self.resource_id,
            backend_id=self.backend_id,
            session_or_thread_id=response.conversation_id,
            workspace_fingerprint=after.sha256,
            source_sha=source_sha,
            checkpoint_id=str(request.payload.get("checkpoint_id") or request.request_id),
            last_successful_turn=(prior.last_successful_turn + 1 if prior else 1),
            last_successful_artifact=last_artifact or (
                prior.last_successful_artifact if prior else None
            ),
            started_at=(prior.started_at if prior else self._now_iso()),
            updated_at=self._now_iso(),
            adapter_contract_version=ADAPTER_CONTRACT_VERSION,
            backend_version=executable.get("version"),
            executable_sha256=executable.get("sha256"),
            auth_mode="subscription",
            objective_id=str(request.payload.get("objective_id") or request.task_id),
            last_terminal_event="result:SUCCESS",
            completed_work_unit_ids=completed_ids,
            completed_work_unit_signatures=signatures,
            artifact_hashes=all_artifacts,
            superseded_session_ids=list(prior.superseded_session_ids if prior else seed.get("superseded_session_ids", [])),
        )
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="antigravity_cli",
            task_id=request.task_id,
            request_id=request.request_id,
            status="SUCCESS",
            exit_code=0,
            workspace=request.workspace,
            changed_paths=changed_paths,
            artifact_hashes=artifacts,
            stdout_digest="Antigravity completed a verified structured turn",
            resource_usage={
                key: value for key, value in response.usage_metadata.items()
                if key in {"input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"}
                and isinstance(value, (int, float))
            },
            evidence_payload={
                "terminal_event": "result",
                "terminal_status": "SUCCESS",
                "resume_mode": "EXACT_SESSION" if prior else "NEW_SESSION",
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            agentic_identity=identity,
            availability=availability,
        )

    def interrupt(self, execution_id: str) -> None:
        adapter = self._adapter
        interrupt = getattr(adapter, "interrupt", None)
        if callable(interrupt):
            interrupt()
        self._active_execution_ids.discard(execution_id)
