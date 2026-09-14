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

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["evidence_class"] = self.evidence_class.value
        return d


class ExecutionBackend(ABC):
    """Abstract execution backend contract."""

    backend_id: str
    trust_zone: ExecutionTrustZone
    supported_capabilities: Set[ExecutionCapability]
    cost: ExecutionCost = ExecutionCost.FREE_LOCAL

    @abstractmethod
    def get_health(self) -> ExecutionHealth:
        """Returns current health/availability status."""
        pass

    @abstractmethod
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Executes the request within bounds or returns failed/denied result."""
        pass
