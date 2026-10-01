"""Provider adequacy evaluation under Resource OS contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from aos.provider_observation import RateLimitObservation
from aos.provider_registry import ProviderEntry
from aos.quota_governor import QuotaDecision


@dataclass(frozen=True)
class ProviderAdequacyDecision:
    provider_id: str
    model_id: str
    eligible: bool
    reasons: Tuple[str, ...]
    request_context_tokens: int
    request_token_budget: int
    model_context_tokens: Optional[int]
    quality_tier: Optional[int]
    minimum_quality: int
    observed_token_limit: Optional[int]
    observed_token_remaining: Optional[int]
    quota_state: str
    quota_reason: str
    scarcity_class: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "request_context_tokens": self.request_context_tokens,
            "request_token_budget": self.request_token_budget,
            "model_context_tokens": self.model_context_tokens,
            "quality_tier": self.quality_tier,
            "minimum_quality": self.minimum_quality,
            "observed_token_limit": self.observed_token_limit,
            "observed_token_remaining": self.observed_token_remaining,
            "quota_state": self.quota_state,
            "quota_reason": self.quota_reason,
            "scarcity_class": self.scarcity_class,
        }


def evaluate_provider_adequacy(
    provider_entry: ProviderEntry,
    resource_requirements: Mapping[str, Any],
    quota_decision: Optional[QuotaDecision],
    rate_limit_observation: Optional[RateLimitObservation] = None,
    *,
    now_epoch: Optional[float] = None,
) -> ProviderAdequacyDecision:
    """Evaluate deterministic provider adequacy against resource requirements and quota state."""
    reasons: list[str] = []
    eligible = True

    context_tokens = int(resource_requirements.get("context_tokens", 0) or 0)
    token_budget = int(
        resource_requirements.get("request_token_budget", 0)
        or (context_tokens + int(resource_requirements.get("output_token_reserve", 2200) or 2200))
    )
    min_quality = int(resource_requirements.get("minimum_quality", 0) or 0)

    model_context = provider_entry.model_context_tokens
    quality_tier = provider_entry.quality_tier
    scarcity_class = provider_entry.scarcity_class or "UNKNOWN"

    quota_state = quota_decision.state if quota_decision is not None else "UNKNOWN"
    quota_reason = quota_decision.reason if quota_decision is not None else "NO_QUOTA_DECISION"

    observed_token_limit = (
        rate_limit_observation.token_limit
        if rate_limit_observation is not None
        else None
    )
    observed_token_remaining = (
        rate_limit_observation.token_remaining
        if rate_limit_observation is not None
        else None
    )

    # Rule A: If model_context_tokens is known and request_token_budget > model_context_tokens
    if model_context is not None and token_budget > model_context:
        eligible = False
        reasons.append("CONTEXT_INADEQUATE")

    # Rule B: If quality_tier is known and quality_tier < minimum_quality
    if quality_tier is not None and quality_tier < min_quality:
        eligible = False
        reasons.append("QUALITY_INADEQUATE")

    # Rule C: If QuotaDecision.eligible is False
    if quota_decision is not None and not quota_decision.eligible:
        eligible = False
        qr = quota_decision.reason
        if qr in ("RATE_LIMITED", "QUOTA_EXHAUSTED", "CREDIT_EXHAUSTED"):
            reasons.append(qr)
        else:
            reasons.append("QUOTA_INELIGIBLE")

    # Rule D: If a current RateLimitObservation has token_limit and request_token_budget > token_limit
    if observed_token_limit is not None and token_budget > observed_token_limit:
        eligible = False
        if "REQUEST_TOKEN_BUDGET_INADEQUATE" not in reasons:
            reasons.append("REQUEST_TOKEN_BUDGET_INADEQUATE")

    # Rule E: If a current observation has token_remaining, and an active token window/reset is still valid,
    # and request_token_budget > token_remaining
    if observed_token_remaining is not None and rate_limit_observation is not None:
        reset_epoch = rate_limit_observation.token_reset_epoch
        window_valid = True
        if reset_epoch is not None and now_epoch is not None:
            window_valid = reset_epoch > now_epoch
        if window_valid and token_budget > observed_token_remaining:
            eligible = False
            if "REQUEST_TOKEN_BUDGET_INADEQUATE" not in reasons:
                reasons.append("REQUEST_TOKEN_BUDGET_INADEQUATE")

    return ProviderAdequacyDecision(
        provider_id=provider_entry.provider_id,
        model_id=provider_entry.model_id,
        eligible=eligible,
        reasons=tuple(reasons),
        request_context_tokens=context_tokens,
        request_token_budget=token_budget,
        model_context_tokens=model_context,
        quality_tier=quality_tier,
        minimum_quality=min_quality,
        observed_token_limit=observed_token_limit,
        observed_token_remaining=observed_token_remaining,
        quota_state=quota_state,
        quota_reason=quota_reason,
        scarcity_class=scarcity_class,
    )
