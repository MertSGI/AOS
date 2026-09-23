"""AOS Autonomous Host V1.

A first-class, restartable, AG-independent host around the accepted Native
Execution Fabric. The host fresh-binds canonical project control state, restores
an exact run plan into a durable DAG, performs model-provider invocation failover,
dispatches native workers, and persists checkpoint / provider-attempt evidence.

Production activation is intentionally out of scope. Antigravity is disabled by
default and can only be enabled explicitly by a future separately-authorized host
profile.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from aos.process_utils import run_headless
from aos.read_identity import build_workspace_source_generation
from aos.quota_governor import QuotaGovernor
from aos.resource_ledger import ResourceEventType, ResourceLedger

from aos.planner import PlannerContractError, PlannerCredentialError, PlannerTransientError
from aos.provider_observation import (
    ContractFailureSubtype,
    RateLimitObservation,
    TaskClass,
    canonical_task_class,
)
from aos.provider_circuit import CircuitState, ProviderCircuitBreakerRegistry
from aos.provider_registry import ProviderRegistry, ProviderRouter, load_routing_policy
from aos.providers import (
    GeminiPlannerProvider,
    GroqPlannerProvider,
    NemotronPlannerProvider,
    OllamaPlannerProvider,
    GenericOpenAICompatiblePlannerProvider,
    FreeLLMAPILocalPlannerProvider,
)
from aos.source_adapter import ProjectSourceAdapter
from aos.validate import validate_file



def _ensure_repo_extensions_importable() -> None:
    """Make the repository-shipped extensions package importable.

    Stable-runtime packaging includes the extensions package plus the hyphenated
    implementation directories as package data. Source checkouts are also
    supported directly.
    """
    candidates: List[Path] = []
    aos_home = os.environ.get("AOS_HOME")
    if aos_home:
        candidates.append(Path(aos_home))
    candidates.extend([
        Path(__file__).resolve().parents[1],
        Path(__file__).resolve().parents[2],
        Path.cwd(),
    ])
    for candidate in candidates:
        if (candidate / "extensions" / "__init__.py").exists():
            resolved = str(candidate.resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return


_ensure_repo_extensions_importable()

from extensions.autonomy_fabric.execution_backend import (  # noqa: E402
    EvidenceClass,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter  # noqa: E402
from extensions.autonomy_fabric.native_workers import (  # noqa: E402
    BrowserExecutionBackend,
    GitHubCIWorker,
    NativeFileWorker,
    NativeGitWorker,
    NativeProcessWorker,
    redact_secrets,
)
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator  # noqa: E402
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, FileRunJournal  # noqa: E402
from extensions.autonomy_fabric.task_dag import NodeGateType, TaskDAG  # noqa: E402


class ProviderAttemptStatus(str, Enum):
    SUCCESS = "SUCCESS"
    NON_RETRYABLE_FAILED = "NON_RETRYABLE_FAILED"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TIMED_OUT = "TIMED_OUT"
    DENIED = "DENIED"


@dataclass
class ProviderAttempt:
    provider_id: str
    model_id: str
    status: ProviderAttemptStatus
    error_class: Optional[str] = None
    message: Optional[str] = None
    timestamp: str = ""
    routed_via: Optional[str] = None
    routed_provider_id: Optional[str] = None
    routed_model_id: Optional[str] = None
    fallback_attempts: Optional[int] = None
    fallback_trail: Optional[str] = None
    task_class: str = TaskClass.UNKNOWN.value
    contract_subtype: Optional[str] = None
    safe_detail: Optional[Dict[str, Any]] = None
    rate_limit_observation: Optional[RateLimitObservation] = None
    quota_decision: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "status": self.status.value,
            "error_class": self.error_class,
            "message": self.message,
            "timestamp": self.timestamp,
            "task_class": self.task_class,
        }
        if self.contract_subtype is not None:
            payload["contract_subtype"] = self.contract_subtype
        if self.safe_detail:
            payload["safe_detail"] = dict(self.safe_detail)
        if self.rate_limit_observation is not None:
            payload["rate_limit_observation"] = self.rate_limit_observation.to_dict()
        if self.quota_decision is not None:
            payload["quota_decision"] = dict(self.quota_decision)
        for key in (
            "routed_via",
            "routed_provider_id",
            "routed_model_id",
            "fallback_attempts",
            "fallback_trail",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


_PROVIDER_FACTORIES: Dict[str, Callable[[str], Any]] = {
    "nemotron": lambda model: NemotronPlannerProvider(model=model),
    "gemini": lambda model: GeminiPlannerProvider(model=model),
    "groq": lambda model: GroqPlannerProvider(model=model),
    "ollama": lambda model: OllamaPlannerProvider(model=model),
}


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: Optional[Path], payload: Dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _bounded_error_message(exc: Exception) -> str:
    # Keep provider attempts useful without persisting raw secret-bearing text.
    return redact_secrets(str(exc))[:500]


def _routing_attempt_fields(provider: Any) -> Dict[str, Any]:
    """Copy only typed, already-sanitized FreeLLMAPI routing metadata."""
    if not isinstance(provider, FreeLLMAPILocalPlannerProvider):
        return {}
    metadata = provider.last_routing_metadata
    if not isinstance(metadata, dict):
        return {}
    allowed = {
        "routed_via",
        "routed_provider_id",
        "routed_model_id",
        "fallback_attempts",
        "fallback_trail",
    }
    return {key: value for key, value in metadata.items() if key in allowed}


class ProviderFailoverReasoningBackend(ExecutionBackend):
    """Model reasoning backend with real post-invocation provider failover.

    Provider-scoped connectivity, credential, capacity, quota, timeout, and
    structured-contract failures route to the next policy-approved provider in
    the same request. Unknown/security failures remain fail-closed.
    """

    backend_id = "provider_failover_reasoning_backend"
    trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
    supported_capabilities = {ExecutionCapability.MODEL_REASONING}
    cost = ExecutionCost.FREE_TIER_CLOUD

    def __init__(
        self,
        provider_router: ProviderRouter,
        provider_factory: Optional[Callable[[str, str], Any]] = None,
        attempt_journal: Optional[Path] = None,
        circuit_registry: Optional[ProviderCircuitBreakerRegistry] = None,
        quota_governor: Optional[QuotaGovernor] = None,
        resource_ledger: Optional[ResourceLedger] = None,
    ) -> None:
        self.provider_router = provider_router
        self.provider_factory = provider_factory or self._default_provider_factory
        self.attempt_journal = attempt_journal
        if circuit_registry is None and attempt_journal is not None:
            circuit_registry = ProviderCircuitBreakerRegistry(
                attempt_journal.parent / "provider-circuits.json"
            )
        self.circuit_registry = circuit_registry
        if resource_ledger is None and attempt_journal is not None:
            resource_dir = attempt_journal.parent / "resource-os"
            resource_ledger = ResourceLedger(
                resource_dir / "resource-ledger.jsonl",
                resource_dir / "resource-ledger-snapshot.json",
            )
        self.resource_ledger = resource_ledger
        if quota_governor is None and attempt_journal is not None:
            quota_governor = QuotaGovernor(
                attempt_journal.parent / "resource-os" / "quota-governor.json",
                ledger=resource_ledger,
            )
        elif quota_governor is not None and resource_ledger is not None:
            quota_governor.ledger = resource_ledger
            quota_governor._reconcile_ledger()
        self.quota_governor = quota_governor

    def _default_provider_factory(self, provider_id: str, model_id: str) -> Any:
        if provider_id == "freellmapi_local":
            entry = self.provider_router.registry.get_provider(provider_id)
            if entry is None:
                raise PlannerContractError("freellmapi_local registry entry is missing")
            return FreeLLMAPILocalPlannerProvider(
                model=entry.model_id,
                base_url=entry.base_url or "http://127.0.0.1:3000/v1",
                credential_env_var=entry.credential_env_var or "FREELLMAPI_LOCAL_API_KEY",
                max_output_tokens=entry.max_output_tokens or 2200,
                readiness_timeout_seconds=entry.readiness_timeout_seconds or 1.5,
            )

        factory = _PROVIDER_FACTORIES.get(provider_id)
        if factory is not None:
            return factory(model_id)

        # Check provider entry from provider_router.registry for generic adapter configuration
        if hasattr(self, "provider_router") and self.provider_router:
            entry = self.provider_router.registry.get_provider(provider_id)
            if entry and (entry.base_url or entry.adapter_type == "GENERIC_OPENAI_COMPATIBLE"):
                return GenericOpenAICompatiblePlannerProvider(
                    provider_id=entry.provider_id,
                    model=entry.model_id,
                    base_url=entry.base_url or "https://api.openai.com/v1",
                    credential_env_var=entry.credential_env_var,
                    api_protocol=entry.api_protocol or "OPENAI_CHAT_COMPLETIONS",
                    max_output_tokens=entry.max_output_tokens or 2200,
                    cloud_local=entry.cloud_local,
                    billing_class=entry.billing_class,
                )

        raise PlannerContractError(f"No executable provider adapter registered for '{provider_id}'")


    def get_health(self) -> ExecutionHealth:
        return ExecutionHealth.HEALTHY

    def _record_attempt(self, attempt: ProviderAttempt) -> None:
        _append_jsonl(self.attempt_journal, attempt.to_dict())

    @staticmethod
    def _ledger_attempt_id(
        request: ExecutionRequest,
        provider_id: str,
        model_id: str,
        task_class: str,
    ) -> str:
        material = "|".join((
            request.request_id,
            request.task_id,
            provider_id,
            model_id,
            task_class,
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _ledger_append(
        self,
        event_type: ResourceEventType,
        attempt_id: str,
        suffix: str,
        payload: Dict[str, Any],
    ) -> None:
        if self.resource_ledger is None:
            return
        self.resource_ledger.append(
            event_type,
            idempotency_key=f"attempt:{attempt_id}:{suffix}",
            payload={"attempt_id": attempt_id, **payload},
        )

    @staticmethod
    def _normalized_usage(usage: Any) -> Dict[str, Any]:
        if not isinstance(usage, dict):
            return {"request_count": 1}
        aliases = {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
        }
        allowed = {
            "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
            "cost_estimate_usd", "cost_actual_usd",
        }
        normalized: Dict[str, Any] = {"request_count": 1}
        for key, value in usage.items():
            target = aliases.get(key, key)
            if target in allowed:
                normalized[target] = value
        return normalized

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        prompt = request.payload.get("prompt", "")
        schema = request.payload.get("schema", {})
        risk_class = request.payload.get("risk_class", "R0")
        task_class = canonical_task_class(
            request.payload.get("task_class", TaskClass.STRUCTURED_PLANNING.value)
        )
        ignore_credentials = bool(request.payload.get("ignore_credentials", False))
        tried: List[str] = []
        attempts: List[ProviderAttempt] = []
        quota_decisions: List[Dict[str, Any]] = []
        failed_provider: Optional[str] = None

        provider_limit = max(1, len(self.provider_router.registry.list_providers()))
        for _ in range(provider_limit):
            route = self.provider_router.select(
                risk_class=risk_class,
                skip_providers=tried,
                ignore_credentials=ignore_credentials,
                post_invocation_failed_provider=failed_provider,
            )
            if route is None:
                break

            provider_id = route.selected_provider_id
            model_id = route.selected_model_id
            tried.append(provider_id)
            ledger_attempt_id = self._ledger_attempt_id(
                request, provider_id, model_id, task_class
            )

            quota_decision = (
                self.quota_governor.decision(provider_id, model_id, task_class)
                if self.quota_governor is not None else None
            )
            if quota_decision is not None:
                quota_decision_payload = {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "task_class": task_class,
                    **quota_decision.to_dict(),
                }
                quota_decisions.append(quota_decision_payload)
                quota_decision_hash = hashlib.sha256(json.dumps(
                    quota_decision_payload, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                self._ledger_append(
                    ResourceEventType.QUOTA_DECISION,
                    ledger_attempt_id,
                    f"quota-decision:{quota_decision_hash}",
                    quota_decision_payload,
                )
                if not quota_decision.eligible:
                    attempt = ProviderAttempt(
                        provider_id=provider_id,
                        model_id=model_id,
                        status=ProviderAttemptStatus.QUOTA_EXHAUSTED,
                        error_class="QUOTA_EXHAUSTED",
                        message=None,
                        timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        task_class=task_class,
                        quota_decision=quota_decision.to_dict(),
                    )
                    attempts.append(attempt)
                    self._record_attempt(attempt)
                    failed_provider = provider_id
                    continue

            half_open_probe = False
            if self.circuit_registry is not None:
                if not self.circuit_registry.is_provider_available(
                    provider_id,
                    model_id=model_id,
                    task_class=task_class,
                ):
                    # Provider circuit is OPEN or already has a leased HALF_OPEN probe.
                    continue
                half_open_probe = (
                    self.circuit_registry.get_health_record(
                        provider_id,
                        model_id=model_id,
                        task_class=task_class,
                    )["circuit_state"] == CircuitState.HALF_OPEN.value
                )

            provider: Any = None
            try:
                self._ledger_append(
                    ResourceEventType.ATTEMPT_STARTED,
                    ledger_attempt_id,
                    "started",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "task_class": task_class,
                        "status": "STARTED",
                    },
                )
                provider = self.provider_factory(provider_id, model_id)
                plan_data, response_id, usage = provider.generate_plan(prompt, schema)
                if not isinstance(plan_data, dict):
                    raise PlannerContractError("Provider response is not a structured object")

                provenance = getattr(provider, "execution_provenance", "LOCAL_OFFLINE")
                evidence_class = (
                    EvidenceClass.LIVE_EXTERNAL_PROOF
                    if provenance == "LIVE_EXTERNAL"
                    else EvidenceClass.LOCAL_RUNTIME_PROOF
                )
                attempt = ProviderAttempt(
                    provider_id=provider_id,
                    model_id=model_id,
                    status=ProviderAttemptStatus.SUCCESS,
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    task_class=task_class,
                    **_routing_attempt_fields(provider),
                )
                attempts.append(attempt)
                self._record_attempt(attempt)
                self._ledger_append(
                    ResourceEventType.ATTEMPT_FINISHED,
                    ledger_attempt_id,
                    "finished",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "task_class": task_class,
                        "status": ProviderAttemptStatus.SUCCESS.value,
                    },
                )
                self._ledger_append(
                    ResourceEventType.RESOURCE_USAGE,
                    ledger_attempt_id,
                    "usage",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "task_class": task_class,
                        **self._normalized_usage(usage),
                    },
                )
                if self.circuit_registry is not None:
                    self.circuit_registry.record_success(
                        provider_id,
                        credential_available=(
                            bool(os.environ.get("FREELLMAPI_LOCAL_API_KEY"))
                            if provider_id == "freellmapi_local" else None
                        ),
                        local_service_available=True if provider_id == "freellmapi_local" else None,
                        model_id=model_id,
                        task_class=task_class,
                    )
                    if half_open_probe:
                        self.circuit_registry.record_probe(provider_id)
                    if len(attempts) > 1:
                        self.circuit_registry.record_failover(provider_id)
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="SUCCESS",
                    exit_code=0,
                    workspace=request.workspace,
                    stdout_digest=f"Structured reasoning succeeded via {provider_id} ({model_id})",
                    evidence_payload={
                        "proposal": plan_data,
                        "provider_route": provider_id,
                        "model_id": model_id,
                        "response_id": response_id,
                        "usage": usage,
                        "provider_attempts": [item.to_dict() for item in attempts],
                        "fallback_used": len(attempts) > 1,
                        "circuit_summary": self.circuit_registry.summarize() if self.circuit_registry else {},
                        "quota_decisions": quota_decisions,
                    },
                    evidence_class=evidence_class,
                )
            except PlannerContractError as exc:
                status = ProviderAttemptStatus.NON_RETRYABLE_FAILED
                error_class = "CONTRACT_FAILURE"
                message = None
                failure_class = "CONTRACT_FAILURE"
                contract_subtype = exc.subtype
                safe_detail = exc.safe_detail
                rate_observation = None
            except PlannerCredentialError as exc:
                status = ProviderAttemptStatus.UNAVAILABLE
                error_class = "CREDENTIAL_UNAVAILABLE"
                message = None
                failure_class = "CREDENTIAL_UNAVAILABLE"
                contract_subtype = None
                safe_detail = None
                rate_observation = None
            except PlannerTransientError as exc:
                raw = str(exc).upper()
                if "LOCAL_GATEWAY_UNAVAILABLE" in raw:
                    status = ProviderAttemptStatus.UNAVAILABLE
                    failure_class = "LOCAL_GATEWAY_UNAVAILABLE"
                elif "LOCAL_GATEWAY_NO_UPSTREAM_ROUTE" in raw:
                    status = ProviderAttemptStatus.UNAVAILABLE
                    failure_class = "LOCAL_GATEWAY_NO_UPSTREAM_ROUTE"
                elif "UPSTREAM_ROUTE_UNAVAILABLE" in raw:
                    status = ProviderAttemptStatus.RETRYABLE_FAILED
                    failure_class = "UPSTREAM_ROUTE_UNAVAILABLE"
                elif "CREDIT_EXHAUSTED" in raw or "PAYMENT REQUIRED" in raw:
                    status = ProviderAttemptStatus.RETRYABLE_FAILED
                    failure_class = "CREDIT_EXHAUSTED"
                elif "QUOTA" in raw or "RESOURCE_EXHAUSTED" in raw:
                    status = ProviderAttemptStatus.QUOTA_EXHAUSTED
                    failure_class = "QUOTA_EXHAUSTED"
                elif "RATE_LIMIT" in raw or "RATE LIMIT" in raw or "429" in raw:
                    status = ProviderAttemptStatus.RETRYABLE_FAILED
                    failure_class = "RATE_LIMITED"
                elif any(code in raw for code in ("CAPACITY", "500", "502", "503", "504", "OVERLOAD")):
                    status = ProviderAttemptStatus.RETRYABLE_FAILED
                    failure_class = "SERVER_CAPACITY"
                else:
                    status = ProviderAttemptStatus.RETRYABLE_FAILED
                    failure_class = "NETWORK_UNAVAILABLE"
                error_class = failure_class
                message = None
                contract_subtype = None
                safe_detail = None
                rate_observation = exc.rate_limit_observation
                if rate_observation is not None and self.quota_governor is not None:
                    quota_decision = self.quota_governor.record(rate_observation)
                    quota_decisions.append({
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "task_class": task_class,
                        **quota_decision.to_dict(),
                    })
            except TimeoutError as exc:
                status = ProviderAttemptStatus.TIMED_OUT
                error_class = "TIMEOUT"
                message = None
                failure_class = "TIMEOUT"
                contract_subtype = None
                safe_detail = None
                rate_observation = None
            except (ConnectionError, OSError) as exc:
                status = ProviderAttemptStatus.UNAVAILABLE
                failure_class = "LOCAL_MODEL_UNAVAILABLE" if provider_id == "ollama" else "NETWORK_UNAVAILABLE"
                error_class = failure_class
                message = None
                contract_subtype = None
                safe_detail = None
                rate_observation = None
            except Exception as exc:
                # Unknown errors are not safe to route around.
                attempt = ProviderAttempt(
                    provider_id=provider_id,
                    model_id=model_id,
                    status=ProviderAttemptStatus.NON_RETRYABLE_FAILED,
                    error_class=exc.__class__.__name__,
                    message=_bounded_error_message(exc),
                    timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    **_routing_attempt_fields(provider),
                )
                attempts.append(attempt)
                self._record_attempt(attempt)
                self._ledger_append(
                    ResourceEventType.ATTEMPT_FINISHED,
                    ledger_attempt_id,
                    "finished",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "task_class": task_class,
                        "status": ProviderAttemptStatus.NON_RETRYABLE_FAILED.value,
                        "failure_family": "UNKNOWN",
                    },
                )
                return ExecutionResult(
                    backend_id=self.backend_id,
                    worker_id="model_reasoner",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="FAILED",
                    exit_code=1,
                    workspace=request.workspace,
                    sanitized_errors=["UNKNOWN_PROVIDER_FAILURE_FAIL_CLOSED"],
                    evidence_payload={
                        "failure_class": "UNKNOWN_PROVIDER_FAILURE_FAIL_CLOSED",
                        "provider_attempts": [item.to_dict() for item in attempts],
                    },
                    evidence_class=EvidenceClass.SOURCE_PROOF,
                )

            attempt = ProviderAttempt(
                provider_id=provider_id,
                model_id=model_id,
                status=status,
                error_class=error_class,
                message=message,
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                task_class=task_class,
                contract_subtype=contract_subtype,
                safe_detail=safe_detail,
                rate_limit_observation=rate_observation,
                quota_decision=(quota_decision.to_dict() if quota_decision is not None else None),
                **_routing_attempt_fields(provider),
            )
            attempts.append(attempt)
            self._record_attempt(attempt)
            self._ledger_append(
                ResourceEventType.ATTEMPT_FINISHED,
                ledger_attempt_id,
                "finished",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "task_class": task_class,
                    "status": status.value,
                    "failure_family": failure_class,
                },
            )
            if self.circuit_registry is not None:
                local_service_available = None
                credential_available = None
                if provider_id == "freellmapi_local":
                    credential_available = bool(os.environ.get("FREELLMAPI_LOCAL_API_KEY"))
                    readiness = getattr(provider, "last_readiness", None)
                    if readiness is not None:
                        local_service_available = bool(readiness.service_available)
                self.circuit_registry.record_failure(
                    provider_id,
                    failure_class,
                    credential_available=credential_available,
                    local_service_available=local_service_available,
                    model_id=model_id,
                    task_class=task_class,
                    rate_limit_observation=rate_observation,
                )
                if half_open_probe:
                    self.circuit_registry.record_probe(provider_id)
            failed_provider = provider_id

        # Only route-eligible providers may influence this request's outage
        # summary and retry deadline. Paid-disabled, missing-credential, or
        # otherwise policy-ineligible providers must not create retry churn.
        circuit_summary = (
            self.circuit_registry.summarize(tried)
            if self.circuit_registry
            else {}
        )
        next_probe_epoch = (
            self.circuit_registry.earliest_next_probe(tried)
            if self.circuit_registry
            else (time.time() + 60.0)
        )
        quota_retry_epoch = (
            self.quota_governor.earliest_retry(tried, task_class=task_class)
            if self.quota_governor is not None else None
        )
        if quota_retry_epoch is not None:
            next_probe_epoch = max(next_probe_epoch, quota_retry_epoch)
        return ExecutionResult(
            backend_id=self.backend_id,
            worker_id="model_reasoner",
            task_id=request.task_id,
            request_id=request.request_id,
            status="DEGRADED",
            exit_code=1,
            workspace=request.workspace,
            sanitized_errors=["ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"],
            evidence_payload={
                "failure_class": "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE",
                "provider_attempts": [item.to_dict() for item in attempts],
                "circuit_summary": circuit_summary,
                "next_probe_at": next_probe_epoch,
                "quota_retry_after_epoch": quota_retry_epoch,
                "quota_decisions": quota_decisions,
                "local_reasoning_result": (
                    "LOCAL_REASONING_UNAVAILABLE"
                    if any(a.provider_id == "ollama" for a in attempts)
                    and not any(a.provider_id == "ollama" and a.status == ProviderAttemptStatus.SUCCESS for a in attempts)
                    else "NOT_SELECTED_OR_NOT_REQUIRED"
                ),
            },
            evidence_class=EvidenceClass.LOCAL_RUNTIME_PROOF,
        )


def probe_ollama_models(base_url: str = "http://localhost:11434", timeout: float = 2.0) -> Dict[str, Any]:
    """Read-only local Ollama probe. Never downloads a model."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [m.get("name") for m in payload.get("models", []) if isinstance(m, dict) and m.get("name")]
        return {"available": True, "models": models}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {"available": False, "models": []}


def refresh_canonical_binding(descriptor_path: Path, binding_path: Path) -> Dict[str, Any]:
    validation, _ = validate_file("project_descriptor", str(descriptor_path))
    if not validation.is_valid:
        raise ValueError(f"Invalid project descriptor: {[str(e) for e in validation.errors]}")
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    adapter = ProjectSourceAdapter(descriptor["repository"], descriptor["control_ref"])
    source_sha = adapter.resolve_ref_to_sha()
    contents, hashes = adapter.fetch_canonical_context(source_sha, descriptor["control"])
    snapshot = adapter.build_normalized_snapshot(
        descriptor["project_id"], source_sha, contents, hashes, descriptor.get("projection")
    )
    if snapshot.get("has_ambiguity"):
        raise RuntimeError(f"Canonical source ambiguity: {snapshot.get('ambiguity_reasons', [])}")

    execution_base_sha = snapshot.get("next_action_execution_base_sha")
    if execution_base_sha:
        adapter.resolve_exact_revision(execution_base_sha)

    binding = {
        "schema_version": "1.0.0",
        "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": descriptor["project_id"],
        "repository": descriptor["repository"],
        "source_ref": descriptor["control_ref"],
        "source_sha": source_sha,
        "input_file_hashes": hashes,
        "current_status": snapshot.get("current_status"),
        "current_milestone": snapshot.get("current_milestone"),
        "canonical_next_action": snapshot.get("canonical_next_action"),
        "target_base_sha": snapshot.get("target_base_sha"),
        "execution_base_sha": execution_base_sha,
    }
    _atomic_json_write(binding_path, binding)
    return binding


def load_bound_run_plan(
    plan_path: Path,
    project_id: str,
    source_sha: str,
    execution_base_sha: Optional[str] = None,
) -> Dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != "1.0.0":
        raise ValueError("Autonomous host run plan schema_version must be 1.0.0")
    if plan.get("project_id") != project_id:
        raise ValueError("Run plan project_id does not match project descriptor")
    if plan.get("bound_source_sha") != source_sha:
        raise ValueError("Run plan is stale: bound_source_sha does not match fresh canonical source")
    if execution_base_sha:
        if plan.get("bound_execution_base_sha") != execution_base_sha:
            raise ValueError(
                "Run plan is stale: bound_execution_base_sha does not match fresh canonical execution base"
            )
    elif plan.get("bound_execution_base_sha"):
        raise ValueError(
            "Run plan declares bound_execution_base_sha but canonical source exposes no execution base"
        )
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Run plan must contain at least one task")
    return plan


def assert_workspace_execution_lineage(workspace: Path, execution_base_sha: Optional[str]) -> str:
    """Fail closed unless the managed workspace is the canonical base or its descendant."""
    res = run_headless(["git", "rev-parse", "HEAD"], cwd=str(workspace), timeout=30)
    if res.returncode != 0:
        raise ValueError(f"Managed workspace HEAD is unavailable: {res.stderr.strip() or res.stdout.strip()}")
    actual_sha = (res.stdout or "").strip()
    if not execution_base_sha or actual_sha == execution_base_sha:
        return actual_sha
    ancestry = run_headless(
        ["git", "merge-base", "--is-ancestor", execution_base_sha, actual_sha],
        cwd=str(workspace),
        timeout=30,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ValueError(
            "Managed workspace is not bound to canonical execution lineage: "
            f"expected base {execution_base_sha}, actual HEAD {actual_sha}"
        )
    return actual_sha


def build_dag(project_id: str, registry: AgentRunRegistry, plan: Dict[str, Any]) -> TaskDAG:
    dag = TaskDAG(project_id, registry)
    for item in plan["tasks"]:
        authority_id = item.get("authority_id")
        if not authority_id or authority_id == "NONE":
            raise ValueError(f"Task {item.get('node_id')} is missing live bounded authority")
        gate_name = item.get("gate_type", "NONE")
        try:
            gate_type = NodeGateType(gate_name)
        except ValueError as exc:
            raise ValueError(f"Unsupported gate_type '{gate_name}'") from exc
        node = dag.add_node(
            node_id=item["node_id"],
            run_type=item["run_type"],
            authority_id=authority_id,
            dependencies=item.get("dependencies", []),
            gate_type=gate_type,
            write_scope=item.get("write_scope", []),
            expected_artifacts=item.get("expected_artifacts", []),
        )
        # PersistentCoordinator intentionally treats payload as an extensible node field.
        node.payload = item.get("payload", {})
    return dag


def build_execution_router(policy_path: Path, runtime_dir: Path) -> ExecutionRouter:
    registry = load_routing_policy(str(policy_path))
    provider_router = ProviderRouter(registry)
    reasoning_backend = ProviderFailoverReasoningBackend(
        provider_router=provider_router,
        attempt_journal=runtime_dir / "provider-attempts.jsonl",
    )
    # Antigravity is deliberately absent. Host V1 proves Zero-AG continuity.
    return ExecutionRouter(
        backends=[
            NativeFileWorker(),
            NativeProcessWorker(),
            NativeGitWorker(),
            GitHubCIWorker(),
            BrowserExecutionBackend(),
            reasoning_backend,
        ],
        ag_required=False,
    )


def run_host(
    descriptor_path: Path,
    plan_path: Path,
    workspace: Path,
    runtime_dir: Path,
    routing_policy_path: Path,
    max_iterations: int = 20,
) -> Dict[str, Any]:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    canonical_binding = refresh_canonical_binding(
        descriptor_path, runtime_dir / "canonical-binding.json"
    )
    assert_workspace_execution_lineage(workspace, canonical_binding.get("execution_base_sha"))
    plan = load_bound_run_plan(
        plan_path,
        canonical_binding["project_id"],
        canonical_binding["source_sha"],
        canonical_binding.get("execution_base_sha"),
    )

    run_journal = FileRunJournal(str(runtime_dir / "run-events.jsonl"))
    registry = AgentRunRegistry(run_journal)
    dag = build_dag(canonical_binding["project_id"], registry, plan)
    router = build_execution_router(routing_policy_path, runtime_dir)
    coordinator = PersistentCoordinator(
        project_id=canonical_binding["project_id"],
        workspace_path=str(workspace),
        dag=dag,
        router=router,
        registry=registry,
        checkpoint_file=str(runtime_dir / "coordinator-checkpoint.json"),
        workspace_source_generation=build_workspace_source_generation(
            project_id=canonical_binding["project_id"],
            canonical_source_sha=canonical_binding["source_sha"],
            canonical_execution_base_sha=canonical_binding.get("execution_base_sha"),
        ),
    )
    state = coordinator.run_until_complete(max_iterations=max_iterations)
    receipt = {
        "schema_version": "1.0.0",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project_id": canonical_binding["project_id"],
        "canonical_source_sha": canonical_binding["source_sha"],
        "canonical_execution_base_sha": canonical_binding.get("execution_base_sha"),
        "completed_task_ids": state.completed_task_ids,
        "failed_task_ids": state.failed_task_ids,
        "completed_read_observations": state.completed_read_observations,
        "iteration_count": state.iteration_count,
        "progress": dag.compute_progress(),
        "ag_backend_enabled": False,
        "ag_invocation_count": 0,
        "production": "NO_GO",
        "ollama_probe": probe_ollama_models(),
    }
    _atomic_json_write(runtime_dir / "host-receipt.json", receipt)
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AOS Autonomous Host V1 (Zero-AG, non-production)")
    parser.add_argument("--project", required=True, help="Project descriptor JSON")
    parser.add_argument(
        "--run-plan",
        help="Optional exact-source-bound manual run plan (debug/test/replay override only)",
    )
    parser.add_argument(
        "--goal",
        default="Continue this project to completion under standing authority.",
        help="Natural-language autonomous project goal used when --run-plan is omitted",
    )
    parser.add_argument(
        "--constraints-json",
        default="[]",
        help="JSON array of project constraints for autonomous mode",
    )
    parser.add_argument(
        "--red-lines-json",
        default="[]",
        help="JSON array of additional red lines for autonomous mode",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Authorized project workspace (defaults to current directory)",
    )
    parser.add_argument(
        "--runtime-dir",
        help="Durable AOS host state directory (defaults inside workspace)",
    )
    parser.add_argument("--max-batches", type=int, default=12)
    parser.add_argument(
        "--routing-policy",
        default="descriptors/nemotron.planner-policy.json",
        help="Provider routing policy JSON",
    )
    parser.add_argument("--max-iterations", type=int, default=20)
    return parser



def hydrate_local_reasoning_credentials() -> Dict[str, bool]:
    # Hydrate provider secrets from the OS vault into this process only.
    # Secret values are not returned, logged, serialized, or written to runtime state.
    try:
        from aos.secure_store import hydrate_environment
    except Exception:
        return {}
    try:
        return hydrate_environment(overwrite=False)
    except Exception:
        return {}

def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    hydrate_local_reasoning_credentials()
    try:
        descriptor_path = Path(args.project).resolve()
        workspace = Path(args.workspace).resolve()
        runtime_dir = (
            Path(args.runtime_dir).resolve()
            if args.runtime_dir
            else (workspace / ".aos-runtime" / "autonomous-project").resolve()
        )
        routing_policy_path = Path(args.routing_policy).resolve()
        if args.run_plan:
            receipt = run_host(
                descriptor_path=descriptor_path,
                plan_path=Path(args.run_plan).resolve(),
                workspace=workspace,
                runtime_dir=runtime_dir,
                routing_policy_path=routing_policy_path,
                max_iterations=args.max_iterations,
            )
        else:
            from aos.planning_kernel import DEFAULT_RED_LINES, run_autonomous_project
            constraints = json.loads(args.constraints_json)
            extra_red_lines = json.loads(args.red_lines_json)
            if not isinstance(constraints, list) or not all(isinstance(x, str) for x in constraints):
                raise ValueError("--constraints-json must be a JSON array of strings")
            if not isinstance(extra_red_lines, list) or not all(isinstance(x, str) for x in extra_red_lines):
                raise ValueError("--red-lines-json must be a JSON array of strings")
            receipt = run_autonomous_project(
                descriptor_path=descriptor_path,
                workspace=workspace,
                runtime_dir=runtime_dir,
                routing_policy_path=routing_policy_path,
                goal=args.goal,
                constraints=tuple(constraints),
                red_lines=tuple(DEFAULT_RED_LINES) + tuple(extra_red_lines),
                max_batches=args.max_batches,
                max_iterations_per_batch=args.max_iterations,
            )
    except Exception as exc:
        print(f"AOS_HOST_HOLD: {_bounded_error_message(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
