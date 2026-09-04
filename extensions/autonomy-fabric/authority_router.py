"""AOS Authority / Human Decision Router (R5).

Categorizes engineering and product decisions into explicit risk classes.
Enforces non-impersonation rules and surfaces human decisions with evidence and options.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class DecisionCategory(str, Enum):
    TECHNICAL_DECISION = "TECHNICAL_DECISION"
    DESIGN_QUALITY_GATE = "DESIGN_QUALITY_GATE"
    HUMAN_PRODUCT_DECISION = "HUMAN_PRODUCT_DECISION"
    AUTHORITY_REQUIRED = "AUTHORITY_REQUIRED"
    PRODUCTION_DECISION = "PRODUCTION_DECISION"


# Categorization map for standard issues
AUTONOMOUS_TECHNICAL_ISSUES = {
    "unsupported_copy",
    "broken_build",
    "generic_design_threshold_failure",
    "missing_screenshot",
    "invalid_state_transition",
    "test_failure",
    "lint_error",
    "type_error",
    "missing_fixture",
}

STRICT_HUMAN_ISSUES = {
    "product_acceptance",
    "irreversible_destructive_decision",
    "production_go",
    "business_legal_decision",
    "credential_provisioning",
    "main_branch_push",
    "production_deploy",
}


@dataclass
class DecisionContext:
    decision_id: str
    category: DecisionCategory
    title: str
    issue_code: str
    description: str
    evidence_ids: List[str] = field(default_factory=list)
    recommended_option: Optional[str] = None
    alternative_options: List[str] = field(default_factory=list)
    consequences: Dict[str, str] = field(default_factory=dict)
    can_autonomously_resolve: bool = False


class AuthorityRouter:
    """Classifies decisions and routes them to autonomous resolution or human escalation."""

    def classify_issue(self, issue_code: str, title: str, description: str) -> DecisionCategory:
        if issue_code in STRICT_HUMAN_ISSUES:
            if issue_code in ("production_go", "production_deploy"):
                return DecisionCategory.PRODUCTION_DECISION
            elif issue_code in ("main_branch_push", "credential_provisioning"):
                return DecisionCategory.AUTHORITY_REQUIRED
            else:
                return DecisionCategory.HUMAN_PRODUCT_DECISION

        if issue_code in AUTONOMOUS_TECHNICAL_ISSUES:
            if "design" in issue_code or "screenshot" in issue_code or "copy" in issue_code:
                return DecisionCategory.DESIGN_QUALITY_GATE
            return DecisionCategory.TECHNICAL_DECISION

        # Default fail-closed to HUMAN_PRODUCT_DECISION if unknown
        return DecisionCategory.HUMAN_PRODUCT_DECISION

    def route_decision(
        self,
        decision_id: str,
        issue_code: str,
        title: str,
        description: str,
        evidence_ids: Optional[List[str]] = None,
        recommended_option: Optional[str] = None,
        alternative_options: Optional[List[str]] = None,
        consequences: Optional[Dict[str, str]] = None,
    ) -> DecisionContext:
        category = self.classify_issue(issue_code, title, description)
        can_autonomously_resolve = category in (
            DecisionCategory.TECHNICAL_DECISION,
            DecisionCategory.DESIGN_QUALITY_GATE,
        )

        return DecisionContext(
            decision_id=decision_id,
            category=category,
            title=title,
            issue_code=issue_code,
            description=description,
            evidence_ids=evidence_ids or [],
            recommended_option=recommended_option,
            alternative_options=alternative_options or [],
            consequences=consequences or {},
            can_autonomously_resolve=can_autonomously_resolve,
        )
