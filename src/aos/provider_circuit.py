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


@dataclass
class ProviderCircuit:
    provider_id: str
    circuit_state: str = CircuitState.CLOSED.value
    consecutive_failure_count: int = 0
    last_failure_class: Optional[str] = None
    last_failure_at: Optional[str] = None
    next_probe_at: Optional[float] = None
    last_success_at: Optional[str] = None
    probe_count: int = 0
    failover_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProviderCircuit:
        return cls(
            provider_id=str(data.get("provider_id", "")),
            circuit_state=str(data.get("circuit_state", CircuitState.CLOSED.value)),
            consecutive_failure_count=int(data.get("consecutive_failure_count", 0)),
            last_failure_class=data.get("last_failure_class"),
            last_failure_at=data.get("last_failure_at"),
            next_probe_at=float(data["next_probe_at"]) if data.get("next_probe_at") is not None else None,
            last_success_at=data.get("last_success_at"),
            probe_count=int(data.get("probe_count", 0)),
            failover_count=int(data.get("failover_count", 0)),
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
        """
        now_epoch = time.time() if now is None else now
        circuit = self.get_circuit(provider_id)

        if circuit.circuit_state == CircuitState.CLOSED.value:
            return True

        if circuit.circuit_state == CircuitState.HALF_OPEN.value:
            return True

        if circuit.circuit_state == CircuitState.OPEN.value:
            if circuit.next_probe_at is not None and now_epoch >= circuit.next_probe_at:
                circuit.circuit_state = CircuitState.HALF_OPEN.value
                self.save()
                return True
            return False

        return True

    def calculate_backoff(self, failure_class: Optional[str], consecutive_failures: int) -> float:
        """Calculate adaptive backoff duration with jitter and failure class awareness."""
        fc = (failure_class or "").upper()
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

    def record_success(self, provider_id: str) -> None:
        """Record a successful reasoning request/probe, closing the circuit."""
        circuit = self.get_circuit(provider_id)
        circuit.circuit_state = CircuitState.CLOSED.value
        circuit.consecutive_failure_count = 0
        circuit.last_failure_class = None
        circuit.next_probe_at = None
        circuit.last_success_at = utc_now()
        self.save()

    def record_failure(
        self,
        provider_id: str,
        failure_class: Optional[str] = None,
        now: Optional[float] = None,
    ) -> float:
        """Record a transient provider failure, incrementing failure count and tripping circuit."""
        now_epoch = time.time() if now is None else now
        circuit = self.get_circuit(provider_id)
        circuit.consecutive_failure_count += 1
        circuit.last_failure_class = failure_class
        circuit.last_failure_at = utc_now()
        
        backoff = self.calculate_backoff(failure_class, circuit.consecutive_failure_count)
        circuit.next_probe_at = now_epoch + backoff
        circuit.circuit_state = CircuitState.OPEN.value
        self.save()
        return circuit.next_probe_at

    def record_probe(self, provider_id: str) -> None:
        circuit = self.get_circuit(provider_id)
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
            if provider_ids
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

    def summarize(self, enabled_providers: Optional[List[str]] = None) -> Dict[str, Any]:
        """Produce clean metrics for self-diagnosis and controller relay."""
        pids = enabled_providers if enabled_providers is not None else list(self._circuits.keys())
        healthy_count = 0
        open_count = 0
        latest_success: Optional[str] = None
        next_probe: Optional[float] = None
        total_probes = 0
        total_failovers = 0

        for pid in pids:
            c = self.get_circuit(pid)
            total_probes += c.probe_count
            total_failovers += c.failover_count
            if c.circuit_state in (CircuitState.CLOSED.value, CircuitState.HALF_OPEN.value):
                healthy_count += 1
            else:
                open_count += 1
                if c.next_probe_at is not None:
                    if next_probe is None or c.next_probe_at < next_probe:
                        next_probe = c.next_probe_at
            if c.last_success_at:
                if latest_success is None or c.last_success_at > latest_success:
                    latest_success = c.last_success_at

        return {
            "healthy_reasoning_provider_count": healthy_count,
            "provider_circuits_open": open_count,
            "all_reasoning_providers_unavailable": healthy_count == 0,
            "next_provider_probe_at": next_probe,
            "last_provider_success": latest_success,
            "provider_probe_count": total_probes,
            "provider_failover_count": total_failovers,
        }
