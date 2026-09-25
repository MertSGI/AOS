"""Bounded loopback Qwen3-4B Q4_K_M reasoning through llama.cpp."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from jsonschema import Draft202012Validator, FormatChecker

from aos.workers.llama_cpp_probe import (
    capability_store_path,
    resolve_capability_status,
    resolve_llama_cpp_identity,
    resolve_qwen_model,
)
from extensions.autonomy_fabric.execution_backend import (
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)


class LlamaCppQwenReasoningBackend(ExecutionBackend):
    backend_id = "qwen3_4b_llama_cpp"
    resource_id = "local_qwen3_4b_q4_k_m_cpu"
    backend_class = BackendClass.REASONING_BACKEND
    trust_zone = ExecutionTrustZone.SANDBOXED_LOCAL
    cost = ExecutionCost.FREE_LOCAL
    supported_capabilities: Set[ExecutionCapability] = {ExecutionCapability.MODEL_REASONING}

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        executable_path: Optional[str] = None,
        model_path: Optional[str] = None,
        capability_status_provider: Optional[Callable[[], str]] = None,
        health_reader: Optional[Callable[[], bool]] = None,
        completion_transport: Optional[Callable[[Dict[str, Any], int], Dict[str, Any]]] = None,
        lifecycle_manager: Optional[Any] = None,
    ) -> None:
        base_url = base_url or os.environ.get(
            "AOS_LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080"
        )
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("llama.cpp backend must use a loopback HTTP endpoint")
        self.base_url = base_url.rstrip("/")
        self.executable_path = executable_path or os.environ.get("AOS_LLAMA_CPP_EXECUTABLE")
        self.model_path = model_path or os.environ.get("AOS_QWEN_MODEL_PATH")
        self._status_provider = capability_status_provider
        self._health_reader = health_reader
        self._transport = completion_transport
        self.lifecycle_manager = lifecycle_manager

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _capability_status(self) -> str:
        if self._status_provider is not None:
            return str(self._status_provider())
        executable = resolve_llama_cpp_identity(self.executable_path or "llama-server")
        model = resolve_qwen_model(self.model_path) if self.model_path else None
        return resolve_capability_status(
            executable=executable, model=model, store_path=capability_store_path()
        )

    def _default_health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=1.0) as response:
                if response.status != 200:
                    return False
                body = response.read(64 * 1024)
            value = json.loads(body.decode("utf-8"))
            return value.get("status") in {"ok", "ready"}
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        status = self._capability_status()
        if status not in {"PROVEN", "TEST_DOUBLE"}:
            return ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now(), source="QWEN_CAPABILITY_ATTESTATION",
                evidence={"capability_status": "UNPROVEN"},
            )
        healthy = self._health_reader() if self._health_reader is not None else self._default_health()
        if not healthy and self.lifecycle_manager is not None:
            snap = self.lifecycle_manager.get_snapshot()
            if snap.state.value in {"STOPPED_READY", "IDLE", "AVAILABLE", "STARTING", "BUSY"}:
                healthy = True
        state = (
            ExecutionAvailabilityState.AVAILABLE
            if healthy else ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE
        )
        return ExecutionAvailabilitySnapshot(
            state, self._now(), source="LLAMA_CPP_LOOPBACK_HEALTH",
            evidence={"profile": "QWEN3_4B_Q4_K_M_CPU16GB", "loopback_only": True},
        )

    def get_health(self) -> ExecutionHealth:
        state = self.get_availability().state
        return ExecutionHealth.HEALTHY if state == ExecutionAvailabilityState.AVAILABLE else ExecutionHealth.UNAVAILABLE

    def _default_transport(self, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "AOS-Qwen-Local/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError("llama.cpp returned non-success status")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > 2 * 1024 * 1024:
                raise RuntimeError("llama.cpp response exceeded bound")
            body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise RuntimeError("llama.cpp response exceeded bound")
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("llama.cpp response must be an object")
        return value

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.lifecycle_manager is not None:
            # Auto-start if stopped or idle
            snap = self.lifecycle_manager.get_snapshot()
            if snap.state.value in {"STOPPED_READY", "IDLE", "STOPPING"}:
                self.lifecycle_manager.start()

        availability = self.get_availability()
        if availability.state != ExecutionAvailabilityState.AVAILABLE:
            return ExecutionResult(
                backend_id=self.backend_id, worker_id="llama_cpp_qwen3_4b",
                task_id=request.task_id, request_id=request.request_id,
                status="DEGRADED", exit_code=1, workspace=request.workspace,
                sanitized_errors=[f"QWEN_{availability.state.value}"],
                availability=availability,
            )
        prompt = request.payload.get("prompt")
        schema = request.payload.get("schema")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12_000:
            return self._contract_failure(request, "QWEN_PROMPT_BOUND", availability)
        if not isinstance(schema, dict):
            return self._contract_failure(request, "QWEN_SCHEMA_REQUIRED", availability)
        encoded_schema = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        if len(encoded_schema) > 16_000:
            return self._contract_failure(request, "QWEN_SCHEMA_BOUND", availability)
        max_tokens = request.payload.get("max_output_tokens", 512)
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 512:
            return self._contract_failure(request, "QWEN_OUTPUT_BOUND", availability)
        payload = {
            "model": "qwen3-4b-q4_k_m",
            "messages": [
                {"role": "system", "content": "Return only one JSON object matching the supplied schema. Do not execute tools or authorize actions."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "seed": 1,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {"name": "aos_bounded_decision", "strict": True, "schema": schema}},
        }
        if self.lifecycle_manager is not None:
            self.lifecycle_manager.mark_request_started()
        try:
            response = (self._transport or self._default_transport)(
                payload, min(max(int(request.timeout_seconds), 1), 120)
            )
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("expected one choice")
            content = (choices[0].get("message") or {}).get("content")
            if not isinstance(content, str) or len(content) > 64_000:
                raise ValueError("invalid bounded content")
            decision = json.loads(content)
            errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(decision))
            if errors:
                raise ValueError("structured output failed schema")
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            safe_usage = {
                key: int(value) for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, int) and value >= 0
            }
        except (OSError, ValueError, TypeError, urllib.error.URLError, json.JSONDecodeError, RuntimeError):
            if self.lifecycle_manager is not None:
                self.lifecycle_manager.mark_request_finished()
            failed = ExecutionAvailabilitySnapshot(
                ExecutionAvailabilityState.CONTRACT_FAILURE,
                self._now(), source="QWEN_STRUCTURED_COMPLETION",
                evidence={"reason": "SANITIZED_CONTRACT_FAILURE"},
            )
            return self._contract_failure(request, "QWEN_STRUCTURED_CONTRACT_FAILURE", failed)
        finally:
            if self.lifecycle_manager is not None:
                self.lifecycle_manager.mark_request_finished()
        result = ExecutionResult(
            backend_id=self.backend_id, worker_id="llama_cpp_qwen3_4b",
            task_id=request.task_id, request_id=request.request_id,
            status="SUCCESS", exit_code=0, workspace=request.workspace,
            stdout_digest="Qwen produced one schema-validated bounded decision",
            resource_usage=safe_usage,
            evidence_payload={
                "output_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "schema_valid": True,
                "profile": "QWEN3_4B_Q4_K_M_CPU16GB",
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            availability=availability,
        )
        result.transient_structured_output = decision
        return result

    def _contract_failure(
        self,
        request: ExecutionRequest,
        reason: str,
        availability: ExecutionAvailabilitySnapshot,
    ) -> ExecutionResult:
        return ExecutionResult(
            backend_id=self.backend_id, worker_id="llama_cpp_qwen3_4b",
            task_id=request.task_id, request_id=request.request_id,
            status="DEGRADED", exit_code=1, workspace=request.workspace,
            sanitized_errors=[reason], evidence_payload={"failure_class": reason},
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
            availability=availability,
        )


def build_llama_server_argv(executable: str, model_path: str, *, port: int = 8080) -> list[str]:
    if not 1024 <= int(port) <= 65535:
        raise ValueError("invalid llama.cpp port")
    model = resolve_qwen_model(model_path)
    if model is None:
        raise ValueError("exact Qwen3-4B Q4_K_M GGUF artifact required")
    return [
        executable, "--model", str(Path(model_path).resolve()),
        "--host", "127.0.0.1", "--port", str(port),
        "--ctx-size", "4096", "--n-predict", "512",
        "--threads", str(min(8, __import__("os").cpu_count() or 1)),
        "--n-gpu-layers", "0",
        "--parallel", "1", "--batch-size", "128", "--ubatch-size", "128",
        "--no-webui", "--jinja",
    ]
