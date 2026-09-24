"""Fail-closed feature, authority, and dynamic-cost policy for decision advice."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RouteDecision:
    eligible: bool
    status: str
    route: Optional[Dict[str, Any]]
    reason: str


class DecisionPolicy:
    def __init__(self, document: Dict[str, Any], *, now: Optional[dt.datetime] = None) -> None:
        self.document = document
        self.now = now or dt.datetime.now(dt.timezone.utc)

    def select_route(
        self, *, risk_class: str, authority_class: str, credential_available: bool
    ) -> RouteDecision:
        if not self.document.get("enabled", False):
            return RouteDecision(False, "UNAVAILABLE", None, "DISABLED")
        if authority_class in set(self.document.get("forbidden_authorities", [])):
            return RouteDecision(False, "REJECTED", None, "FORBIDDEN_AUTHORITY")
        if risk_class not in set(self.document.get("allowed_risk_classes", [])):
            return RouteDecision(False, "REJECTED", None, "RISK_CLASS_BLOCKED")
        if not credential_available:
            return RouteDecision(False, "UNAVAILABLE", None, "CREDENTIAL_UNAVAILABLE")
        for route in self.document.get("routes", []):
            if route.get("cost_class") != "PROMOTIONAL_FREE":
                continue
            if route.get("zero_price_observed") is not True:
                continue
            try:
                expiry = dt.datetime.fromisoformat(str(route["not_after"]).replace("Z", "+00:00"))
                observed = dt.datetime.fromisoformat(str(route["price_observed_at"]).replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                continue
            if self.now > expiry or observed > self.now:
                continue
            return RouteDecision(True, "AVAILABLE", dict(route), "PROMOTIONAL_FREE_PROVEN")
        return RouteDecision(False, "COST_BLOCKED", None, "NO_ZERO_COST_ROUTE")
