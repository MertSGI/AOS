"""Deterministic capability, adequacy, scarcity, and cost resource ranking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from extensions.autonomy_fabric.execution_backend import (
    BackendClass, ExecutionAvailabilityState, ExecutionBackend, ExecutionCost, ExecutionHealth,
    ExecutionRequest,
)


@dataclass(frozen=True)
class ResourceRank:
    backend_id: str
    eligible: bool
    score: int
    reasons: tuple[str, ...]


class ResourceOrchestrator:
    """Ranks only deterministically eligible resources; paid is never a default escape."""

    _COST = {
        ExecutionCost.FREE_LOCAL: 10,
        ExecutionCost.FREE_TIER_CLOUD: 30,
        ExecutionCost.SUBSCRIPTION_INCLUDED: 50,
        ExecutionCost.QUOTA_LIMITED: 90,
        ExecutionCost.PAID_CLOUD: 1000,
    }

    def rank(self, backends: Iterable[ExecutionBackend], request: ExecutionRequest) -> List[ResourceRank]:
        required = set(request.required_capabilities)
        requirements = request.payload.get("resource_requirements", {})
        requirements = requirements if isinstance(requirements, dict) else {}
        context_tokens = int(requirements.get("context_tokens", 0) or 0)
        minimum_quality = int(requirements.get("minimum_quality", 0) or 0)
        maximum_latency = int(requirements.get("maximum_latency_ms", 0) or 0)
        complexity_class = str(requirements.get("complexity_class", "")).upper()
        local_qwen_allowed = requirements.get("local_qwen_allowed")
        agentic_planning_allowed = requirements.get("agentic_planning_allowed")
        scarcity_policy = str(requirements.get("scarcity_policy", "ALLOW_SCARCE")).upper()

        ranked: List[ResourceRank] = []
        for backend in backends:
            reasons: list[str] = []
            eligible = True
            if not required.issubset(backend.supported_capabilities):
                eligible = False
                reasons.append("CAPABILITY_MISMATCH")
            if backend.cost == ExecutionCost.PAID_CLOUD:
                eligible = False
                reasons.append("PAID_DEFAULT_DENIED")
            if backend.get_health() in {ExecutionHealth.UNAVAILABLE, ExecutionHealth.QUOTA_EXHAUSTED}:
                eligible = False
                reasons.append("HEALTH_UNAVAILABLE")
            availability = None
            getter = getattr(backend, "get_availability", None)
            if callable(getter):
                availability = getter()
                if availability.state not in {
                    ExecutionAvailabilityState.AVAILABLE,
                    ExecutionAvailabilityState.LOW_OR_SCARCE,
                }:
                    eligible = False
                    reasons.append(f"AVAILABILITY_{availability.state.value}")
                elif availability.state == ExecutionAvailabilityState.LOW_OR_SCARCE and scarcity_policy == "AVOID_SCARCE":
                    eligible = False
                    reasons.append("SCARCITY_POLICY_AVOIDED")

            # Local Qwen envelope check
            is_local_qwen = "qwen" in backend.backend_id.lower() or (
                backend.cost == ExecutionCost.FREE_LOCAL
                and getattr(backend, "backend_class", None) == BackendClass.REASONING_BACKEND
            )
            if is_local_qwen and local_qwen_allowed is False:
                eligible = False
                reasons.append("LOCAL_QWEN_DISALLOWED")

            # Agentic planning bridge envelope check
            is_agentic_bridge = "planning_bridge" in backend.backend_id.lower() or hasattr(backend, "underlying_backend")
            if is_agentic_bridge and agentic_planning_allowed is False:
                eligible = False
                reasons.append("AGENTIC_PLANNING_DISALLOWED")

            capacity = int(getattr(backend, "context_window_tokens", 0) or 0)
            if context_tokens and capacity and context_tokens > capacity:
                eligible = False
                reasons.append("CONTEXT_INADEQUATE")
            quality = int(getattr(backend, "quality_tier", 1) or 1)
            if minimum_quality and quality < minimum_quality:
                eligible = False
                reasons.append("QUALITY_INADEQUATE")
            latency = int(getattr(backend, "expected_latency_ms", 0) or 0)
            if maximum_latency and latency and latency > maximum_latency:
                eligible = False
                reasons.append("LATENCY_INADEQUATE")
            score = self._COST.get(backend.cost, 500)
            if availability is not None and availability.state == ExecutionAvailabilityState.LOW_OR_SCARCE:
                score += 25
                reasons.append("SCARCE")
            if request.agentic_identity is not None:
                if request.agentic_identity.backend_id == backend.backend_id:
                    score -= 5
                    reasons.append("COMPATIBLE_SESSION_LOCALITY")
                else:
                    score += 5
                    reasons.append("CROSS_RESOURCE_HANDOFF")
            ranked.append(ResourceRank(backend.backend_id, eligible, score, tuple(reasons)))
        return sorted(ranked, key=lambda item: (not item.eligible, item.score, item.backend_id))

    def select(self, backends: Iterable[ExecutionBackend], request: ExecutionRequest) -> Optional[str]:
        return next((item.backend_id for item in self.rank(backends, request) if item.eligible), None)
