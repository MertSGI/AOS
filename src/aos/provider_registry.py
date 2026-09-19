"""AOS Provider Registry and Deterministic Router."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aos.planner import PlannerProvider
from aos.validate import validate_file


@dataclass
class ProviderEntry:
    """Describes a single provider's capabilities and policy."""
    provider_id: str
    model_id: str
    credential_env_var: Optional[str]
    billing_class: str
    structured_output: bool
    cloud_local: str
    enabled: bool
    allowed_data_classifications: List[str]
    display_name: Optional[str] = None
    provider_console_url: Optional[str] = None
    adapter_type: Optional[str] = None
    api_protocol: Optional[str] = None
    base_url: Optional[str] = None
    additional_nonsecret_env_vars: Optional[List[str]] = None
    priority: Optional[int] = None
    capabilities: Optional[List[str]] = None
    max_output_tokens: Optional[int] = None


@dataclass
class ProviderExecutionContext:
    """Immutable execution context for an authenticated/selected provider."""
    provider_id: str
    model_id: str
    billing_class: str
    data_classification: str
    selection_reason: str
    fallback_used: bool = False
    fallback_from: Optional[str] = None


@dataclass
class RoutingResult:
    """Result of deterministic provider selection."""
    context: ProviderExecutionContext
    selected_provider_id: str
    selected_model_id: str
    selection_reason: str
    fallback_used: bool = False
    fallback_from: Optional[str] = None


class ProviderRegistry:
    """Registry describing provider capabilities from routing policy."""

    def __init__(self, policy_data: Dict[str, Any]):
        self.routing_mode = policy_data["routing_mode"]
        self.allow_paid_fallback = bool(policy_data.get("allow_paid_fallback", False))
        self.paid_fallback_enabled = bool(policy_data.get("paid_fallback_enabled", False))
        self.paid_daily_budget_usd = float(policy_data.get("paid_daily_budget_usd", 0.0) or 0.0)
        self.paid_monthly_budget_usd = float(policy_data.get("paid_monthly_budget_usd", 0.0) or 0.0)
        self.allow_provider_fallback = bool(policy_data.get("allow_provider_fallback", True))
        self.data_classification = policy_data["data_classification"]
        self.risk_routes = policy_data["risk_routes"]
        self._providers: Dict[str, ProviderEntry] = {}
        for key, pdata in policy_data.get("providers", {}).items():
            self._providers[key] = ProviderEntry(
                provider_id=pdata["provider_id"],
                model_id=pdata["model_id"],
                credential_env_var=pdata.get("credential_env_var"),
                billing_class=pdata["billing_class"],
                structured_output=pdata["structured_output"],
                cloud_local=pdata["cloud_local"],
                enabled=pdata["enabled"],
                allowed_data_classifications=pdata["allowed_data_classifications"],
                display_name=pdata.get("display_name"),
                provider_console_url=pdata.get("provider_console_url"),
                adapter_type=pdata.get("adapter_type"),
                api_protocol=pdata.get("api_protocol"),
                base_url=pdata.get("base_url"),
                additional_nonsecret_env_vars=pdata.get("additional_nonsecret_env_vars"),
                priority=pdata.get("priority"),
                capabilities=pdata.get("capabilities"),
                max_output_tokens=pdata.get("max_output_tokens"),
            )


    def get_provider(self, provider_id: str) -> Optional[ProviderEntry]:
        return self._providers.get(provider_id)

    def list_providers(self) -> List[ProviderEntry]:
        return list(self._providers.values())


class ProviderRouter:
    """Deterministic provider selection based on policy, risk and data classification."""

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def select(
        self,
        risk_class: str = "R0",
        skip_providers: Optional[List[str]] = None,
        ignore_credentials: bool = False,
        post_invocation_failed_provider: Optional[str] = None,
    ) -> Optional[RoutingResult]:
        """Select the first eligible provider for a risk class and data classification.

        Returns None if no provider is eligible (PROVIDER_POLICY_HOLD).
        """
        skip = set(skip_providers or [])
        route = self.registry.risk_routes.get(risk_class)
        if not route:
            return None

        preferred = route.get("preferred_providers", [])
        data_class = self.registry.data_classification

        # Post-invocation fallback only applies if a prior provider was actually invoked and failed transiently
        is_post_invocation_fallback = (
            post_invocation_failed_provider is not None
            and self.registry.allow_provider_fallback
        )

        for provider_id in preferred:
            if provider_id in skip:
                continue

            entry = self.registry.get_provider(provider_id)
            if entry is None:
                continue

            if not entry.enabled:
                continue

            if not entry.structured_output:
                continue

            if data_class not in entry.allowed_data_classifications:
                continue

            if entry.billing_class == "PAID":
                paid_eligible = (
                    self.registry.allow_paid_fallback
                    and self.registry.paid_fallback_enabled
                    and self.registry.paid_daily_budget_usd > 0
                    and self.registry.paid_monthly_budget_usd > 0
                )
                if not paid_eligible:
                    continue

            if not ignore_credentials and entry.cloud_local == "CLOUD" and entry.credential_env_var:
                if not os.environ.get(entry.credential_env_var):
                    # Eligibility filtering skip - NOT a post-invocation fallback
                    continue

            reason = f"Selected '{entry.provider_id}' ({entry.model_id}): "
            reason += f"billing={entry.billing_class}, data={data_class}, risk={risk_class}"
            if is_post_invocation_fallback:
                reason += f", fallback_from='{post_invocation_failed_provider}'"

            ctx = ProviderExecutionContext(
                provider_id=entry.provider_id,
                model_id=entry.model_id,
                billing_class=entry.billing_class,
                data_classification=data_class,
                selection_reason=reason,
                fallback_used=is_post_invocation_fallback,
                fallback_from=post_invocation_failed_provider if is_post_invocation_fallback else None,
            )

            return RoutingResult(
                context=ctx,
                selected_provider_id=entry.provider_id,
                selected_model_id=entry.model_id,
                selection_reason=reason,
                fallback_used=is_post_invocation_fallback,
                fallback_from=post_invocation_failed_provider if is_post_invocation_fallback else None,
            )

        return None


def load_routing_policy(policy_path: str) -> ProviderRegistry:
    """Load and validate a routing policy file, returning a ProviderRegistry."""
    res, code = validate_file("planner_routing_policy", policy_path)
    if not res.is_valid:
        raise ValueError(f"Invalid routing policy '{policy_path}': {[str(e) for e in res.errors]}")
    with open(policy_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ProviderRegistry(data)
