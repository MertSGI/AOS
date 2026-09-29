"""Authoritative, provider-neutral Resource OS availability snapshots."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from aos.runtime_contract import CONTRACT_VERSION, utc_now
from aos.runtime_store import atomic_json, exclusive_file_lock, read_json


UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ResourceSnapshot:
    resource_id: str
    resource_class: str
    capability: tuple[str, ...]
    health: str
    credential_status: str
    local_service_status: str
    quota_state: str
    retry_after_epoch: Optional[float]
    scarcity: str
    cost_class: str
    context_capacity: Optional[int]
    quality_history: str
    expected_latency_ms: Optional[int]
    workspace_session_compatibility: str
    provenance: str
    observed_at: str
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["capability"] = list(self.capability)
        return value


class ResourceSnapshotStore:
    """Persists one honest current observation per registered resource."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @staticmethod
    def observe_backend(backend: Any) -> ResourceSnapshot:
        health = UNKNOWN
        get_health = getattr(backend, "get_health", None)
        if callable(get_health):
            observed = get_health()
            health = str(getattr(observed, "value", observed) or UNKNOWN)

        availability = None
        get_availability = getattr(backend, "get_availability", None)
        if callable(get_availability):
            availability = get_availability()
        state = str(getattr(getattr(availability, "state", None), "value", UNKNOWN))
        evidence = dict(getattr(availability, "evidence", {}) or {})
        capabilities = tuple(sorted(
            str(getattr(item, "value", item))
            for item in (getattr(backend, "supported_capabilities", set()) or set())
        ))
        backend_class = getattr(backend, "backend_class", None)
        cost = getattr(backend, "cost", None)
        retry = getattr(availability, "retry_after_epoch", None)
        credential = evidence.get("credential_status")
        if credential is None and "credential_available" in evidence:
            credential = "AVAILABLE" if evidence["credential_available"] else "UNAVAILABLE"
        service = evidence.get("local_service_status")
        if service is None and "local_service_available" in evidence:
            service = "AVAILABLE" if evidence["local_service_available"] else "UNAVAILABLE"
        return ResourceSnapshot(
            resource_id=str(getattr(backend, "backend_id", UNKNOWN)),
            resource_class=str(getattr(backend_class, "value", backend_class) or UNKNOWN),
            capability=capabilities,
            health=health,
            credential_status=str(credential or UNKNOWN),
            local_service_status=str(service or UNKNOWN),
            quota_state=(
                state if state in {"QUOTA_EXHAUSTED", "LOW_OR_SCARCE", "AVAILABLE"} else UNKNOWN
            ),
            retry_after_epoch=float(retry) if retry is not None else None,
            scarcity=("SCARCE" if state == "LOW_OR_SCARCE" else ("NOT_SCARCE" if state == "AVAILABLE" else UNKNOWN)),
            cost_class=str(getattr(cost, "value", cost) or UNKNOWN),
            context_capacity=(
                int(getattr(backend, "context_window_tokens"))
                if getattr(backend, "context_window_tokens", None) is not None
                else None
            ),
            quality_history=str(evidence.get("quality_history") or UNKNOWN),
            expected_latency_ms=(
                int(getattr(backend, "expected_latency_ms"))
                if getattr(backend, "expected_latency_ms", None) is not None
                else None
            ),
            workspace_session_compatibility=str(
                evidence.get("workspace_session_compatibility") or UNKNOWN
            ),
            provenance=str(getattr(availability, "source", UNKNOWN) or UNKNOWN),
            observed_at=str(getattr(availability, "observed_at", "") or utc_now()),
            evidence=evidence,
        )

    def record(self, snapshots: Iterable[ResourceSnapshot]) -> Dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            current = read_json(self.path, {})
            resources = current.get("resources")
            if not isinstance(resources, dict):
                resources = {}
            for snapshot in snapshots:
                resources[snapshot.resource_id] = snapshot.to_dict()
            document = {
                "contract_version": CONTRACT_VERSION,
                "observed_at": utc_now(),
                "unknown_policy": "PRESERVE_UNKNOWN_NEVER_FABRICATE_HEALTH",
                "resources": resources,
            }
            atomic_json(self.path, document)
            return document

    def observe_and_record(self, backends: Iterable[Any]) -> Dict[str, ResourceSnapshot]:
        observations = [self.observe_backend(backend) for backend in backends]
        self.record(observations)
        return {item.resource_id: item for item in observations}

    def read(self) -> Dict[str, Any]:
        return read_json(self.path, {
            "contract_version": CONTRACT_VERSION,
            "observed_at": "",
            "unknown_policy": "PRESERVE_UNKNOWN_NEVER_FABRICATE_HEALTH",
            "resources": {},
        })
