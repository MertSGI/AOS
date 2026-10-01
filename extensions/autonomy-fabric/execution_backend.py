"""AOS Generic Execution Backend Contracts & Envelopes (V2).

Defines provider-neutral execution interfaces, request envelopes, capability sets,
health and trust zones, and normalized execution results.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Set
import datetime
import uuid


class ExecutionCapability(str, Enum):
    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    PATCH_APPLY = "PATCH_APPLY"
    PROCESS_EXEC = "PROCESS_EXEC"
    GIT_READ = "GIT_READ"
    GIT_WRITE = "GIT_WRITE"
    GITHUB_READ = "GITHUB_READ"
    CI_OBSERVE = "CI_OBSERVE"
    BROWSER = "BROWSER"
    MODEL_REASONING = "MODEL_REASONING"
    VISUAL_REASONING = "VISUAL_REASONING"
    ANTIGRAVITY = "ANTIGRAVITY"
    TEST_EXECUTION = "TEST_EXECUTION"
    LONG_HORIZON_AGENTIC_WORK = "LONG_HORIZON_AGENTIC_WORK"


class BackendClass(str, Enum):
    NATIVE_EXECUTION_BACKEND = "NATIVE_EXECUTION_BACKEND"
    REASONING_BACKEND = "REASONING_BACKEND"
    AGENTIC_EXECUTION_BACKEND = "AGENTIC_EXECUTION_BACKEND"


class ExecutionTrustZone(str, Enum):
    SANDBOXED_LOCAL = "SANDBOXED_LOCAL"
    RESTRICTED_WORKSPACE = "RESTRICTED_WORKSPACE"
    HOST_USER = "HOST_USER"
    REMOTE_CI = "REMOTE_CI"


class ExecutionHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    UNAVAILABLE = "UNAVAILABLE"


class ExecutionCost(str, Enum):
    FREE_LOCAL = "FREE_LOCAL"
    FREE_TIER_CLOUD = "FREE_TIER_CLOUD"
    PAID_CLOUD = "PAID_CLOUD"
    QUOTA_LIMITED = "QUOTA_LIMITED"
    SUBSCRIPTION_INCLUDED = "SUBSCRIPTION_INCLUDED"


class ExecutionAvailabilityState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOW_OR_SCARCE = "LOW_OR_SCARCE"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    CONTRACT_FAILURE = "CONTRACT_FAILURE"
    UNKNOWN = "UNKNOWN"


class ExecutionFailureDisposition(str, Enum):
    """Routing meaning of a normalized backend failure.

    Resource availability failures remain retryable after the finite route is
    exhausted.  Backend-local failures may be routed around, but must never be
    relabeled as a project-wide resource outage.  Authority, security, and
    request-contract failures stop routing immediately.
    """

    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    BACKEND_LOCAL = "BACKEND_LOCAL"
    FAIL_CLOSED = "FAIL_CLOSED"


@dataclass(frozen=True)
class ExecutionAvailabilitySnapshot:
    state: ExecutionAvailabilityState
    observed_at: str
    retry_after_epoch: Optional[float] = None
    source: str = "OBSERVED_RUNTIME"
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class AgenticSessionIdentity:
    """AOS-owned resume identity; external session state is never authoritative."""

    resource_id: str
    backend_id: str
    workspace_fingerprint: str
    source_sha: str
    checkpoint_id: str
    session_or_thread_id: Optional[str] = None
    last_successful_turn: int = 0
    last_successful_artifact: Optional[Dict[str, str]] = None
    started_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    adapter_contract_version: str = "1.0.0"
    backend_version: Optional[str] = None
    executable_sha256: Optional[str] = None
    auth_mode: Optional[str] = None
    objective_id: Optional[str] = None
    last_terminal_event: Optional[str] = None
    completed_work_unit_ids: List[str] = field(default_factory=list)
    completed_work_unit_signatures: Dict[str, str] = field(default_factory=dict)
    artifact_hashes: Dict[str, str] = field(default_factory=dict)
    superseded_session_ids: List[str] = field(default_factory=list)
    schema_version: str = "1.0.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EvidenceClass(str, Enum):
    SOURCE_PROOF = "SOURCE_PROOF"
    LOCAL_RUNTIME_PROOF = "LOCAL_RUNTIME_PROOF"
    CI_RUNTIME_PROOF = "CI_RUNTIME_PROOF"
    LIVE_EXTERNAL_PROOF = "LIVE_EXTERNAL_PROOF"


@dataclass
class ExecutionRequest:
    """Bounded, machine-readable envelope for executable tasks."""
    task_id: str
    project_id: str
    workspace: str
    operation_class: str
    required_capabilities: List[ExecutionCapability]
    authority_id: str
    read_scope: List[str] = field(default_factory=list)
    write_scope: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    network_policy: str = "DENY_ALL"  # DENY_ALL, LOCAL_ONLY, APPROVED_ENDPOINTS
    data_classification: str = "PUBLIC"  # PUBLIC, INTERNAL, CONFIDENTIAL
    credential_refs: List[str] = field(default_factory=list)
    timeout_seconds: int = 180
    expected_artifacts: List[str] = field(default_factory=list)
    expected_changed_paths: List[str] = field(default_factory=list)
    rollback_policy: str = "FAIL_CLOSED_ROLLBACK"
    evidence_requirements: Dict[str, Any] = field(default_factory=dict)
    payload: Dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: f"req-{uuid.uuid4().hex[:10]}")
    agentic_identity: Optional[AgenticSessionIdentity] = None
    context_pack: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["required_capabilities"] = [c.value for c in self.required_capabilities]
        return d


@dataclass
class ExecutionResult:
    """Normalized result emitted by every execution backend."""
    backend_id: str
    worker_id: str
    task_id: str
    request_id: str
    status: str  # SUCCESS, FAILED, TIMED_OUT, DENIED, DEGRADED
    exit_code: Optional[int]
    workspace: str
    changed_paths: List[str] = field(default_factory=list)
    artifact_hashes: Dict[str, str] = field(default_factory=dict)
    stdout_digest: str = ""
    stderr_digest: str = ""
    sanitized_errors: List[str] = field(default_factory=list)
    resource_usage: Dict[str, Any] = field(default_factory=dict)
    evidence_class: EvidenceClass = EvidenceClass.LOCAL_RUNTIME_PROOF
    started_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    finished_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    evidence_payload: Dict[str, Any] = field(default_factory=dict)
    agentic_identity: Optional[AgenticSessionIdentity] = None
    availability: Optional[ExecutionAvailabilitySnapshot] = None
    transient_structured_output: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("transient_structured_output", None)
        d["evidence_class"] = self.evidence_class.value
        if self.availability is not None:
            d["availability"]["state"] = self.availability.state.value
        return d


_FAIL_CLOSED_FAILURE_TOKENS = (
    "AUTHORITY",
    "SECURITY",
    "WRITE_SCOPE_VIOLATION",
    "PLANNING_WRITE_AUTHORITY_DENIED",
    "PLANNING_MUTATION_DETECTED",
    "COMPLETED_WORK_MUST_NOT_BE_DUPLICATED",
    "INVALID_PLANNING_SCHEMA",
    "UNKNOWN_PROVIDER_FAILURE_FAIL_CLOSED",
)

_BACKEND_LOCAL_FAILURE_TOKENS = (
    "PRELAUNCH_CONTRACT_FAILURE",
    "CONTRACT_FAILURE",
    "MALFORMED_STRUCTURED_OUTPUT",
    "SCHEMA_VALIDATION_FAILED",
    "STALE_AGENT_SESSION",
    "EXECUTABLE_UNAVAILABLE",
    "ADAPTER_CONTRACT",
    "EXECUTION_NONZERO_EXIT",
    "INCOMPLETE_OUTCOME",
)

_RESOURCE_UNAVAILABLE_FAILURE_TOKENS = (
    "ALL_ELIGIBLE_REASONING_PROVIDER",
    "ALL_ELIGIBLE_REASONING_RESOURCES_UNAVAILABLE",
    "QUOTA",
    "RATE_LIMIT",
    "CAPACITY",
    "NETWORK_UNAVAILABLE",
    "TEMPORARILY_UNAVAILABLE",
    "PROVIDER_UNREACHABLE",
    "LOCAL_MODEL_UNAVAILABLE",
    "LOCAL_REASONING_SERVICE_UNAVAILABLE",
    "LOCAL_GATEWAY_UNAVAILABLE",
    "LOCAL_GATEWAY_NO_UPSTREAM_ROUTE",
    "UPSTREAM_ROUTE_UNAVAILABLE",
    "CREDIT_EXHAUSTED",
    "AUTH_UNAVAILABLE",
    "CREDENTIAL_UNAVAILABLE",
    "TIMEOUT",
)


def execution_failure_class(result: Any) -> str:
    """Return one sanitized, normalized failure class for routing decisions."""
    payload = getattr(result, "evidence_payload", {})
    if not isinstance(payload, dict):
        payload = {}
    value = payload.get("failure_class")
    if not value:
        errors = getattr(result, "sanitized_errors", [])
        if isinstance(errors, list) and errors:
            value = errors[0]
    return str(value or getattr(result, "status", "UNKNOWN")).upper()


def classify_execution_failure(result: Any) -> ExecutionFailureDisposition:
    """Classify a failed result at the explicit failover/wait boundary."""
    status = str(getattr(result, "status", "UNKNOWN")).upper()
    failure_class = execution_failure_class(result)

    if any(token in failure_class for token in _FAIL_CLOSED_FAILURE_TOKENS):
        return ExecutionFailureDisposition.FAIL_CLOSED
    if any(token in failure_class for token in _BACKEND_LOCAL_FAILURE_TOKENS):
        return ExecutionFailureDisposition.BACKEND_LOCAL

    availability = getattr(result, "availability", None)
    availability_state = getattr(availability, "state", None)
    try:
        availability_state = ExecutionAvailabilityState(availability_state)
    except (TypeError, ValueError):
        availability_state = None
    if availability_state in {
        ExecutionAvailabilityState.QUOTA_EXHAUSTED,
        ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
        ExecutionAvailabilityState.AUTH_UNAVAILABLE,
    }:
        return ExecutionFailureDisposition.RESOURCE_UNAVAILABLE
    if availability_state == ExecutionAvailabilityState.CONTRACT_FAILURE:
        return ExecutionFailureDisposition.BACKEND_LOCAL

    if status == "WAITING_FOR_REASONING_PROVIDER" or any(
        token in failure_class for token in _RESOURCE_UNAVAILABLE_FAILURE_TOKENS
    ):
        return ExecutionFailureDisposition.RESOURCE_UNAVAILABLE
    if status == "DEGRADED":
        # A bare DEGRADED result is safe to route around, but cannot prove a
        # global resource outage without an availability classification.
        return ExecutionFailureDisposition.BACKEND_LOCAL
    return ExecutionFailureDisposition.FAIL_CLOSED


class ExecutionBackend(ABC):
    """Abstract execution backend contract."""

    backend_id: str
    trust_zone: ExecutionTrustZone
    supported_capabilities: Set[ExecutionCapability]
    cost: ExecutionCost = ExecutionCost.FREE_LOCAL
    backend_class: BackendClass = BackendClass.NATIVE_EXECUTION_BACKEND

    @abstractmethod
    def get_health(self) -> ExecutionHealth:
        """Returns current health/availability status."""
        pass

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Executes the request within bounds or returns failed/denied result."""
        pass


class AgenticExecutionBackend(ExecutionBackend):
    """Shared contract for resumable external agentic execution resources."""

    backend_class = BackendClass.AGENTIC_EXECUTION_BACKEND

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        context_pack = dict(request.context_pack or {})
        if request.agentic_identity is not None:
            return self.resume(request, request.agentic_identity, context_pack)
        return self.start(request, context_pack)

    @abstractmethod
    def get_availability(self) -> ExecutionAvailabilitySnapshot:
        pass

    @abstractmethod
    def start(
        self, request: ExecutionRequest, context_pack: Dict[str, Any]
    ) -> ExecutionResult:
        pass

    @abstractmethod
    def resume(
        self,
        request: ExecutionRequest,
        identity: AgenticSessionIdentity,
        context_pack: Dict[str, Any],
    ) -> ExecutionResult:
        pass

    @abstractmethod
    def interrupt(self, execution_id: str) -> None:
        pass
