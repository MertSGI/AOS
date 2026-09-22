"""AOS Durable Provider Circuit Breaker Registry.

Implements provider-aware circuit breaking with adaptive backoff,
preventing fixed-interval retry storms while allowing independent healthy
providers to immediately bypass tripped providers.

Circuit States:
- CLOSED: Provider is healthy; all requests pass through.
- OPEN: Provider has failed repeatedly; requests fail fast or are bypassed.
- HALF_OPEN: Test probe permitted to check if provider has recovered.
"""
from __future__ import annotations

import datetime as dt
import enum
import json
import os
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aos.runtime_contract import utc_now
from aos.runtime_store import atomic_json, read_json


class CircuitState(str, enum.Enum):
    UNKNOWN = "UNKNOWN"
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# Adaptive backoff tiers in seconds (bounded maximum 1800s = 30m)
BACKOFF_TIERS = [
    60.0,    # 1st transient failure: ~1 min
    120.0,   # 2nd transient failure: ~2 min
    300.0,   # 3rd transient failure: ~5 min
    900.0,   # 4th persistent failure: ~15 min
    1800.0,  # 5th+ or quota/capacity exhausted: ~30 min max
]
MAX_BACKOFF_SECONDS = 1800.0
HALF_OPEN_PROBE_LEASE_SECONDS = 120.0
CREDIT_EXHAUSTED_COOLDOWN_SECONDS = 24 * 60 * 60.0


def _timestamp_epoch(value: Optional[str]) -> float:
    if not value:
        return float("-inf")
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


@dataclass
class ProviderCircuit:
    provider_id: str
    circuit_state: str = CircuitState.UNKNOWN.value
    consecutive_failure_count: int = 0
    last_failure_class: Optional[str] = None
    last_failure_at: Optional[str] = None
    next_probe_at: Optional[float] = None
    last_success_at: Optional[str] = None
    last_observed_at: Optional[str] = None
    last_probe_status: Optional[str] = None
    latency_ms: Optional[int] = None
    credential_available: Optional[bool] = None
    local_service_available: Optional[bool] = None
    probe_count: int = 0
    failover_count: int = 0
    half_open_probe_started_at: Optional[float] = None
    probe_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderCircuit:
        return cls(
            provider_id=str(data.get("provider_id", "")),
            circuit_state=str(data.get("circuit_state", CircuitState.UNKNOWN.value)),
            consecutive_failure_count=int(data.get("consecutive_failure_count", 0)),
            last_failure_class=data.get("last_failure_class"),
            last_failure_at=data.get("last_failure_at"),
            next_probe_at=float(data["next_probe_at"]) if data.get("next_probe_at") is not None else None,
            last_success_at=data.get("last_success_at"),
            last_observed_at=data.get("last_observed_at"),
            last_probe_status=data.get("last_probe_status"),
            latency_ms=int(data["latency_ms"]) if data.get("latency_ms") is not None else None,
            credential_available=(
                bool(data["credential_available"])
                if data.get("credential_available") is not None else None
            ),
            local_service_available=(
                bool(data["local_service_available"])
                if data.get("local_service_available") is not None else None
            ),
            probe_count=int(data.get("probe_count", 0)),
            failover_count=int(data.get("failover_count", 0)),
            half_open_probe_started_at=(
                float(data["half_open_probe_started_at"])
                if data.get("half_open_probe_started_at") is not None else None
            ),
            probe_ids=[str(item) for item in data.get("probe_ids", []) if item],
        )


class ProviderCircuitBreakerRegistry:
    """Durable registry of provider circuit breakers."""

    def __init__(self, persistence_path: Optional[Path] = None) -> None:
        self.persistence_path = persistence_path
        self._circuits: Dict[str, ProviderCircuit] = {}
        self.load()

    def load(self) -> None:
        if not self.persistence_path or not self.persistence_path.exists():
            return
        data = read_json(self.persistence_path)
        circuits_data = data.get("circuits", {})
        if isinstance(circuits_data, dict):
            for pid, cdata in circuits_data.items():
                if isinstance(cdata, dict):
                    self._circuits[pid] = ProviderCircuit.from_dict(cdata)

    def save(self) -> None:
        if not self.persistence_path:
            return
        payload = {
            "schema_version": "1.0.0",
            "updated_at": utc_now(),
            "circuits": {pid: c.to_dict() for pid, c in self._circuits.items()},
        }
        atomic_json(self.persistence_path, payload)

    def get_circuit(self, provider_id: str) -> ProviderCircuit:
        if provider_id not in self._circuits:
            self._circuits[provider_id] = ProviderCircuit(provider_id=provider_id)
        return self._circuits[provider_id]

    def is_provider_available(self, provider_id: str, now: Optional[float] = None) -> bool:
        """Check whether provider is available for reasoning requests.
        
        If OPEN and probe interval elapsed, transitions to HALF_OPEN.
        UNKNOWN is treated as available (never probed yet).
        """
        now_epoch = time.time() if now is None else now
        circuit = self.get_circuit(provider_id)

        if circuit.circuit_state in (CircuitState.CLOSED.value, CircuitState.UNKNOWN.value):
            return True

        if circuit.circuit_state == CircuitState.HALF_OPEN.value:
            started = circuit.half_open_probe_started_at
            if started is not None and now_epoch < started + HALF_OPEN_PROBE_LEASE_SECONDS:
                return False
            circuit.half_open_probe_started_at = now_epoch
            self.save()
            return True

        if circuit.circuit_state == CircuitState.OPEN.value:
            if circuit.next_probe_at is not None and now_epoch >= circuit.next_probe_at:
                circuit.circuit_state = CircuitState.HALF_OPEN.value
                circuit.half_open_probe_started_at = now_epoch
                self.save()
                return True
            return False

        return True

    def is_probe_due(self, provider_id: str, now: Optional[float] = None) -> bool:
        """Return whether a synthetic probe may run without changing circuit state."""
        now_epoch = time.time() if now is None else now
        circuit = self.get_circuit(provider_id)
        if circuit.circuit_state == CircuitState.CLOSED.value:
            return False
        if circuit.circuit_state == CircuitState.UNKNOWN.value:
            return circuit.credential_available is not False and circuit.local_service_available is not False
        if circuit.circuit_state == CircuitState.OPEN.value:
            return circuit.next_probe_at is None or now_epoch >= circuit.next_probe_at
        if circuit.circuit_state == CircuitState.HALF_OPEN.value:
            started = circuit.half_open_probe_started_at
            return started is None or now_epoch >= started + HALF_OPEN_PROBE_LEASE_SECONDS
        return False

    def calculate_backoff(self, failure_class: Optional[str], consecutive_failures: int) -> float:
        """Calculate adaptive backoff duration with jitter and failure class awareness."""
        fc = (failure_class or "").upper()
        if "CREDIT_EXHAUSTED" in fc or "PAYMENT_REQUIRED" in fc:
            return CREDIT_EXHAUSTED_COOLDOWN_SECONDS
        if any(token in fc for token in ("QUOTA", "RATE_LIMIT", "429", "RESOURCE_EXHAUSTED", "CAPACITY")):
            # Quota or capacity exhaustion: backoff immediately to longer tier (15-30 mins)
            base = 1800.0 if consecutive_failures >= 2 else 900.0
        else:
            tier_idx = min(max(0, consecutive_failures - 1), len(BACKOFF_TIERS) - 1)
            base = BACKOFF_TIERS[tier_idx]

        # Bounded jitter: ±10% to prevent synchronized retry thundering herds
        jitter = base * random.uniform(-0.10, 0.10)
        backoff = max(30.0, min(MAX_BACKOFF_SECONDS, base + jitter))
        return backoff

    def record_success(
        self,
        provider_id: str,
        *,
        observed_at: Optional[str] = None,
        probe_status: str = "PASS",
        latency_ms: Optional[int] = None,
        credential_available: Optional[bool] = None,
        local_service_available: Optional[bool] = None,
    ) -> None:
        """Record a successful reasoning request/probe, closing the circuit."""
        circuit = self.get_circuit(provider_id)
        circuit.circuit_state = CircuitState.CLOSED.value
        circuit.consecutive_failure_count = 0
        circuit.next_probe_at = None
        circuit.half_open_probe_started_at = None
        circuit.last_success_at = observed_at or utc_now()
        circuit.last_observed_at = circuit.last_success_at
        circuit.last_probe_status = probe_status
        circuit.latency_ms = latency_ms
        circuit.credential_available = credential_available
        circuit.local_service_available = local_service_available
        self.save()

    def record_failure(
        self,
        provider_id: str,
        failure_class: Optional[str] = None,
        now: Optional[float] = None,
        observed_at: Optional[str] = None,
        probe_status: str = "FAIL",
        latency_ms: Optional[int] = None,
        credential_available: Optional[bool] = None,
        local_service_available: Optional[bool] = None,
    ) -> float:
        """Record a transient provider failure, incrementing failure count and tripping circuit."""
        now_epoch = time.time() if now is None else now
        circuit = self.get_circuit(provider_id)
        circuit.consecutive_failure_count += 1
        circuit.last_failure_class = failure_class
        circuit.last_failure_at = observed_at or utc_now()
        circuit.last_observed_at = circuit.last_failure_at
        circuit.last_probe_status = probe_status
        circuit.latency_ms = latency_ms
        circuit.credential_available = credential_available
        circuit.local_service_available = local_service_available
        
        backoff = self.calculate_backoff(failure_class, circuit.consecutive_failure_count)
        circuit.next_probe_at = now_epoch + backoff
        circuit.circuit_state = CircuitState.OPEN.value
        circuit.half_open_probe_started_at = None
        self.save()
        return circuit.next_probe_at

    def record_observation(
        self,
        provider_id: str,
        *,
        observed_at: Optional[str] = None,
        probe_status: str = "NOT_ATTEMPTED",
        latency_ms: Optional[int] = None,
        credential_available: Optional[bool] = None,
        local_service_available: Optional[bool] = None,
    ) -> None:
        """Persist availability metadata without fabricating health or a failure."""
        circuit = self.get_circuit(provider_id)
        circuit.last_observed_at = observed_at or utc_now()
        circuit.last_probe_status = probe_status
        circuit.latency_ms = latency_ms
        circuit.credential_available = credential_available
        circuit.local_service_available = local_service_available
        self.save()

    def record_probe(self, provider_id: str, probe_id: Optional[str] = None) -> None:
        circuit = self.get_circuit(provider_id)
        if probe_id:
            if probe_id in circuit.probe_ids:
                return
            circuit.probe_ids.append(probe_id)
        circuit.probe_count += 1
        self.save()

    def record_failover(self, provider_id: str) -> None:
        circuit = self.get_circuit(provider_id)
        circuit.failover_count += 1
        self.save()

    def earliest_next_probe(self, provider_ids: Optional[List[str]] = None, now: Optional[float] = None) -> float:
        """Calculate the earliest next probe epoch across the requested or all registered circuits."""
        now_epoch = time.time() if now is None else now
        relevant_circuits = (
            [self.get_circuit(pid) for pid in provider_ids]
            if provider_ids is not None
            else list(self._circuits.values())
        )
        probes = [
            c.next_probe_at
            for c in relevant_circuits
            if c.circuit_state == CircuitState.OPEN.value and c.next_probe_at is not None
        ]
        if probes:
            return max(now_epoch + 30.0, min(probes))
        return now_epoch + 60.0

    def summarize(
        self,
        enabled_providers: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Produce truthful metrics without treating UNKNOWN/HALF_OPEN as healthy."""
        now_epoch = time.time() if now is None else now
        pids = enabled_providers if enabled_providers is not None else list(self._circuits.keys())
        healthy_count = 0
        probe_eligible_count = 0
        unknown_count = 0
        open_count = 0
        latest_success: Optional[str] = None
        next_probe: Optional[float] = None
        total_probes = 0
        total_failovers = 0

        for pid in pids:
            c = self.get_circuit(pid)
            total_probes += c.probe_count
            total_failovers += c.failover_count
            if c.circuit_state == CircuitState.CLOSED.value:
                healthy_count += 1
            elif c.circuit_state == CircuitState.UNKNOWN.value:
                unknown_count += 1
                if c.credential_available is not False and c.local_service_available is not False:
                    probe_eligible_count += 1
            elif c.circuit_state == CircuitState.HALF_OPEN.value:
                started = c.half_open_probe_started_at
                if started is None or now_epoch >= started + HALF_OPEN_PROBE_LEASE_SECONDS:
                    probe_eligible_count += 1
            elif c.circuit_state == CircuitState.OPEN.value:
                open_count += 1
                if c.next_probe_at is not None:
                    if next_probe is None or c.next_probe_at < next_probe:
                        next_probe = c.next_probe_at
                    if now_epoch >= c.next_probe_at:
                        probe_eligible_count += 1
            if c.last_success_at:
                if _timestamp_epoch(c.last_success_at) > _timestamp_epoch(latest_success):
                    latest_success = c.last_success_at

        return {
            "healthy_reasoning_provider_count": healthy_count,
            "probe_eligible_reasoning_provider_count": probe_eligible_count,
            "unknown_reasoning_provider_count": unknown_count,
            "provider_circuits_open": open_count,
            "all_reasoning_providers_unavailable": (
                healthy_count == 0 and probe_eligible_count == 0 and len(pids) > 0
            ),
            "next_provider_probe_at": next_probe,
            "last_provider_success": latest_success,
            "provider_probe_count": total_probes,
            "provider_failover_count": total_failovers,
        }

    def per_provider_details(
        self, enabled_providers: Optional[List[str]] = None,
        credential_status: Optional[Dict[str, bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Return sanitized per-provider circuit detail for health/relay."""
        pids = enabled_providers if enabled_providers is not None else list(self._circuits.keys())
        cred = credential_status or {}
        details: List[Dict[str, Any]] = []
        for pid in pids:
            c = self.get_circuit(pid)
            details.append({
                "provider": pid,
                "circuit_state": c.circuit_state,
                "credential_available": (
                    cred.get(pid) if pid in cred else c.credential_available
                ),
                "local_service_available": c.local_service_available,
                "last_failure_class": c.last_failure_class,
                "last_failure_at": c.last_failure_at,
                "next_probe_at": c.next_probe_at,
                "last_success_at": c.last_success_at,
                "last_observed_at": c.last_observed_at,
                "probe_status": c.last_probe_status,
                "latency_ms": c.latency_ms,
                "probe_count": c.probe_count,
                "failover_count": c.failover_count,
                "consecutive_failure_count": c.consecutive_failure_count,
            })
        return details

    @classmethod
    def aggregate_registries(
        cls,
        registries: List["ProviderCircuitBreakerRegistry"],
        *,
        enabled_providers: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> "ProviderCircuitBreakerRegistry":
        """Aggregate command-local evidence using the newest authoritative event.

        A stale CLOSED observation never overrides a newer failure from another
        command.  The returned registry is read-only/in-memory and is not a
        competing persistence source.
        """
        merged = cls(persistence_path=None)
        now_epoch = time.time() if now is None else now
        all_providers: Dict[str, List[ProviderCircuit]] = {}
        for reg in registries:
            for pid, circuit in reg._circuits.items():
                all_providers.setdefault(pid, []).append(circuit)

        for pid in enabled_providers or []:
            all_providers.setdefault(pid, [])

        for pid, circuits in all_providers.items():
            if not circuits:
                merged._circuits[pid] = ProviderCircuit(provider_id=pid)
                continue

            unique_probe_ids = {
                probe_id
                for circuit in circuits
                for probe_id in circuit.probe_ids
            }
            legacy_probe_count = sum(
                max(0, circuit.probe_count - len(circuit.probe_ids))
                for circuit in circuits
            )
            total_probes = legacy_probe_count + len(unique_probe_ids)
            total_failovers = sum(c.failover_count for c in circuits)

            success_circuits = [c for c in circuits if c.last_success_at]
            latest_success_circuit = (
                max(success_circuits, key=lambda c: _timestamp_epoch(c.last_success_at))
                if success_circuits else None
            )
            latest_success = latest_success_circuit.last_success_at if latest_success_circuit else None

            failure_circuits = [c for c in circuits if c.last_failure_at]
            latest_failure_circuit = (
                max(failure_circuits, key=lambda c: _timestamp_epoch(c.last_failure_at))
                if failure_circuits else None
            )
            latest_failure = latest_failure_circuit.last_failure_at if latest_failure_circuit else None
            latest_failure_epoch = _timestamp_epoch(latest_failure)
            latest_success_epoch = _timestamp_epoch(latest_success)
            if latest_success_epoch == float("-inf") and latest_failure_epoch == float("-inf"):
                state = CircuitState.UNKNOWN.value
                last_observed = None
                next_probe = None
            elif latest_success_epoch > latest_failure_epoch:
                state = CircuitState.CLOSED.value
                last_observed = latest_success
                next_probe = None
            else:
                assert latest_failure_circuit is not None
                next_probe = latest_failure_circuit.next_probe_at
                state = (
                    CircuitState.HALF_OPEN.value
                    if next_probe is None or now_epoch >= next_probe
                    else CircuitState.OPEN.value
                )
                last_observed = latest_failure

            latest_observation_circuit = max(
                circuits,
                key=lambda c: max(
                    _timestamp_epoch(c.last_success_at),
                    _timestamp_epoch(c.last_failure_at),
                    _timestamp_epoch(c.last_observed_at),
                ),
            )

            merged._circuits[pid] = ProviderCircuit(
                provider_id=pid,
                circuit_state=state,
                consecutive_failure_count=(
                    0 if state == CircuitState.CLOSED.value
                    else latest_failure_circuit.consecutive_failure_count if latest_failure_circuit else 0
                ),
                last_failure_class=latest_failure_circuit.last_failure_class if latest_failure_circuit else None,
                last_failure_at=latest_failure,
                next_probe_at=next_probe,
                last_success_at=latest_success,
                last_observed_at=last_observed,
                last_probe_status=latest_observation_circuit.last_probe_status,
                latency_ms=latest_observation_circuit.latency_ms,
                credential_available=latest_observation_circuit.credential_available,
                local_service_available=latest_observation_circuit.local_service_available,
                probe_count=total_probes,
                failover_count=total_failovers,
                half_open_probe_started_at=(
                    latest_failure_circuit.half_open_probe_started_at
                    if state == CircuitState.HALF_OPEN.value and latest_failure_circuit else None
                ),
                probe_ids=sorted(unique_probe_ids),
            )
        return merged
