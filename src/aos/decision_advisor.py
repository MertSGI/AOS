"""Provider-neutral, advisory-only decision contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Protocol, Tuple


class DecisionPrimitive(str, Enum):
    CHOICE = "CHOICE"
    SCORE = "SCORE"
    BOOLEAN_PROBABILITY = "BOOLEAN_PROBABILITY"


class DecisionStatus(str, Enum):
    ADVISED = "ADVISED"
    ABSTAINED = "ABSTAINED"
    UNAVAILABLE = "UNAVAILABLE"
    REJECTED = "REJECTED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class DecisionQuestion:
    primitive: DecisionPrimitive
    prompt: str
    options: Tuple[str, ...] = ()
    levels: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionRequest:
    request_id: str
    objective_id: str
    task_class: str
    decision_kind: str
    state: Any
    questions: Dict[str, DecisionQuestion]
    eligible_option_ids: Tuple[str, ...]
    required_evidence_ids: Tuple[str, ...]
    data_classification: str
    risk_class: str
    source_sha: str
    context_fingerprint: str
    max_context_tokens: int = 4096
    deadline_ms: int = 1000
    authority_class: str = "TECHNICAL_ADVISORY"


@dataclass(frozen=True)
class DecisionConfidence:
    kind: str
    selected_probability: float | None
    distribution_confidence: float | None
    probabilities: Dict[str, float]
    policy_threshold: float | None
    band: str
    threshold_policy_id: str = "jev-thresholds-v1"
    calibrated_on_aos_dataset: bool = False


@dataclass(frozen=True)
class DecisionCostObservation:
    route_id: str
    cost_class: str
    input_usd_per_million: float | None
    output_usd_per_million: float | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    observed_cost_usd: float | None = None
    price_observed_at: str = ""
    price_source: str = "POLICY"
    paid_authorized: bool = False


@dataclass(frozen=True)
class DecisionResourceAvailability:
    route_id: str
    model_requested: str
    model_resolved: str | None
    status: str
    credential_available: bool
    zero_cost_eligible: bool
    next_retry_at: str | None = None
    observation_id: str | None = None


@dataclass(frozen=True)
class DecisionResult:
    request_id: str
    status: DecisionStatus
    answers: Dict[str, Any]
    confidence: Dict[str, DecisionConfidence]
    recommended_option_ids: Tuple[str, ...]
    model_resolved: str | None
    availability: DecisionResourceAvailability
    cost: DecisionCostObservation
    advisory_only: bool = True
    sanitized_error_class: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


class DecisionAdvisor(Protocol):
    def advise(self, request: DecisionRequest) -> DecisionResult: ...


def unavailable_result(request_id: str, reason: str = "DECISION_ADVISOR_DISABLED") -> DecisionResult:
    return DecisionResult(
        request_id=request_id,
        status=DecisionStatus.UNAVAILABLE,
        answers={}, confidence={}, recommended_option_ids=(), model_resolved=None,
        availability=DecisionResourceAvailability(
            route_id="none", model_requested="jev-1.13.0", model_resolved=None,
            status="UNAVAILABLE", credential_available=False, zero_cost_eligible=False,
        ),
        cost=DecisionCostObservation(
            route_id="none", cost_class="UNKNOWN",
            input_usd_per_million=None, output_usd_per_million=None,
        ),
        sanitized_error_class=reason,
    )


class NullDecisionAdvisor:
    def advise(self, request: DecisionRequest) -> DecisionResult:
        return unavailable_result(request.request_id)
