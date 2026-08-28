"""Deterministic Human Gate Policy Engine for AOS-DEC-022."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List, Optional, Set

from aos.validate import validate_document

DEC_022_SCHEMA_VERSION = "0.1.0"

HUMAN_CRITICAL_CATEGORIES: Set[str] = {
    "MATERIAL_ROADMAP_OR_SCOPE_CHANGE",
    "PRODUCTION_OR_CUSTOMER_MUTATION",
    "DESTRUCTIVE_DATA_OPERATION",
    "SECURITY_OR_AUTH_BOUNDARY_CHANGE",
    "TRUST_MODEL_CHANGE",
    "SECRET_OR_SENSITIVE_DATA_BOUNDARY_CHANGE",
    "BILLING_OR_PAID_PROVIDER_ACTIVATION",
    "AUTHORITATIVE_EVIDENCE_CONTRADICTION",
    "EVIDENCE_WAIVER_OR_FORCE_PASS",
    "RETRY_CEILING_EXCEEDED_WITH_NO_SAFE_REMEDIATION",
    "LEGAL_OR_COMPLIANCE_AUTHORITY_AMBIGUITY",
    "UNAPPROVED_HOST_PERMISSION_OR_TRUST_CONFIGURATION_CHANGE",
}

FORBIDDEN_CATEGORIES: Set[str] = {
    "GUARD_BYPASS",
    "DANGEROUS_PERMISSION_BYPASS",
    "FORCE_PASS_EVIDENCE",
    "VERIFICATION_DISABLE",
    "UNAUTHORIZED_DESTRUCTIVE_PRODUCTION_MUTATION",
    "SECRET_EXFILTRATION",
    "FAIL_CLOSED_WEAKENING",
}

NON_HUMAN_GATED_FAILURES: Set[str] = {
    "ORDINARY_IMPLEMENTATION_FAILURE",
    "DETERMINISTIC_TEST_FAILURE",
    "CI_FAILURE",
    "WORKER_TIMEOUT",
    "WORKER_SEMANTIC_FAILURE",
    "MISSING_EVIDENCE_ARTIFACT",
    "MALFORMED_EVIDENCE_ARTIFACT",
    "CAPABILITY_REPROBE",
    "STALE_EXECUTION_REVISION",
    "ORDINARY_RETRY_WITHIN_BUDGET",
    "CONTROLLED_DIAGNOSTIC_OPERATION",
    "SAFE_CAPABILITY_REFRESH",
    "NORMAL_TOOLCHAIN_IDENTITY_REFRESH",
}

ROUTINE_RETRY_CEILING = 2


class HumanGateDecisionResult:
    """Encapsulates the output of a deterministic human gate policy evaluation."""

    def __init__(
        self,
        decision: str,
        authority_source: str,
        reason_codes: List[str],
        human_critical_categories: Optional[List[str]] = None,
        forbidden_categories: Optional[List[str]] = None,
        execution_authorization_doc: Optional[Dict[str, Any]] = None,
    ):
        self.decision = decision
        self.authority_source = authority_source
        self.reason_codes = reason_codes
        self.human_critical_categories = human_critical_categories or []
        self.forbidden_categories = forbidden_categories or []
        self.execution_authorization_doc = execution_authorization_doc

    def to_dict(self) -> Dict[str, Any]:
        res: Dict[str, Any] = {
            "decision": self.decision,
            "authority_source": self.authority_source,
            "reason_codes": self.reason_codes,
            "human_critical_categories": self.human_critical_categories,
            "forbidden_categories": self.forbidden_categories,
        }
        if self.execution_authorization_doc:
            res["execution_authorization"] = self.execution_authorization_doc
        return res


def evaluate_human_gate_policy(
    task: Dict[str, Any],
    project_descriptor: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> HumanGateDecisionResult:
    """Evaluates deterministic human gate policy under AOS-DEC-022."""
    context = context or {}
    reason_codes: List[str] = []
    detected_critical: Set[str] = set()
    detected_forbidden: Set[str] = set()

    risk_class = task.get("risk_class", "R1")
    task_id = task.get("task_id", "UNKNOWN_TASK")
    project_id = task.get("project_id", "aos")
    gate = task.get("gate", "AOS-4")
    base_sha = task.get("base_sha")

    # 1. Detect explicit human-critical categories from context or task
    ctx_critical = context.get("human_critical_categories", [])
    for cat in ctx_critical:
        if cat in HUMAN_CRITICAL_CATEGORIES:
            detected_critical.add(cat)

    # 2. Detect explicit forbidden categories from context or task
    ctx_forbidden = context.get("forbidden_categories", [])
    for cat in ctx_forbidden:
        if cat in FORBIDDEN_CATEGORIES or cat:
            detected_forbidden.add(cat)

    # Context flags mapping to forbidden categories
    if context.get("bypass_guard") or context.get("force_pass") or context.get("disable_verification"):
        if context.get("bypass_guard"):
            detected_forbidden.add("GUARD_BYPASS")
        if context.get("force_pass"):
            detected_forbidden.add("FORCE_PASS_EVIDENCE")
        if context.get("disable_verification"):
            detected_forbidden.add("VERIFICATION_DISABLE")

    # Context flags mapping to critical categories
    if context.get("evidence_contradiction"):
        detected_critical.add("AUTHORITATIVE_EVIDENCE_CONTRADICTION")
    if context.get("evidence_waiver"):
        detected_critical.add("EVIDENCE_WAIVER_OR_FORCE_PASS")
    if context.get("unapproved_host_config"):
        detected_critical.add("UNAPPROVED_HOST_PERMISSION_OR_TRUST_CONFIGURATION_CHANGE")

    # Retry context evaluation
    retry_count = context.get("retry_count", 0)
    retry_max = context.get("retry_max", ROUTINE_RETRY_CEILING)
    has_safe_remediation = context.get("has_safe_remediation", True)

    if retry_count > retry_max and not has_safe_remediation:
        detected_critical.add("RETRY_CEILING_EXCEEDED_WITH_NO_SAFE_REMEDIATION")

    # Safe capability reprobe special handling
    is_capability_reprobe = context.get("is_capability_reprobe", False)
    reprobe_allowed = context.get("reprobe_allowed", True)

    # --- DETERMINISTIC DECISION TREE ---

    # 1. FORBIDDEN check (highest priority)
    if detected_forbidden:
        reason_codes.append("FORBIDDEN_CATEGORY_DETECTED")
        for f_cat in sorted(list(detected_forbidden)):
            reason_codes.append(f"FORBIDDEN:{f_cat}")
        decision = "FORBIDDEN"
        authority_source = "NONE"

    # 2. R4 check
    elif risk_class == "R4":
        reason_codes.append("RISK_CLASS_R4_PERMANENTLY_FORBIDDEN")
        decision = "FORBIDDEN"
        authority_source = "NONE"

    # 3. Explicit Human Critical Category
    elif detected_critical:
        reason_codes.append("HUMAN_CRITICAL_CATEGORY_DETECTED")
        for c_cat in sorted(list(detected_critical)):
            reason_codes.append(f"CRITICAL:{c_cat}")
        decision = "HUMAN_REQUIRED"
        authority_source = "NONE"

    # 4. R3 check
    elif risk_class == "R3":
        reason_codes.append("RISK_CLASS_R3_REQUIRES_HUMAN_APPROVAL")
        decision = "HUMAN_REQUIRED"
        authority_source = "NONE"

    # 5. Safe Capability Reprobe (AUTO_REMEDIATE)
    elif is_capability_reprobe and reprobe_allowed:
        reason_codes.append("SAFE_CAPABILITY_REPROBE_AUTO_REMEDIATE")
        decision = "AUTO_REMEDIATE"
        authority_source = "POLICY_AUTONOMOUS"

    # 6. R2 check (Scope dependent)
    elif risk_class == "R2":
        is_isolated = context.get("is_isolated_non_prod", False)
        is_accepted_envelope = context.get("is_accepted_envelope", False)
        if is_isolated and is_accepted_envelope:
            reason_codes.append("RISK_CLASS_R2_ACCEPTED_ISOLATED_AUTO_EXECUTE")
            decision = "AUTO_EXECUTE"
            authority_source = "POLICY_AUTONOMOUS"
        else:
            reason_codes.append("RISK_CLASS_R2_OUTSIDE_ACCEPTED_ENVELOPE_REQUIRES_HUMAN")
            decision = "HUMAN_REQUIRED"
            authority_source = "NONE"

    # 7. R1 check (Isolated non-prod)
    elif risk_class == "R1":
        is_isolated = context.get("is_isolated_non_prod")
        if is_isolated is True:
            reason_codes.append("RISK_CLASS_R1_ISOLATED_NONPROD_AUTO_EXECUTE")
            decision = "AUTO_EXECUTE"
            authority_source = "POLICY_AUTONOMOUS"
        elif is_isolated is False:
            reason_codes.append("RISK_CLASS_R1_EXPLICIT_NON_ISOLATED_REQUIRES_HUMAN")
            decision = "HUMAN_REQUIRED"
            authority_source = "NONE"
        else:
            reason_codes.append("RISK_CLASS_R1_MISSING_ISOLATION_EVIDENCE_FAILS_CLOSED")
            decision = "HUMAN_REQUIRED"
            authority_source = "NONE"

    # 8. R0 check
    elif risk_class == "R0":
        reason_codes.append("RISK_CLASS_R0_AUTO_EXECUTE")
        decision = "AUTO_EXECUTE"
        authority_source = "POLICY_AUTONOMOUS"

    else:
        reason_codes.append(f"UNKNOWN_RISK_CLASS_{risk_class}_REQUIRES_HUMAN")
        decision = "HUMAN_REQUIRED"
        authority_source = "NONE"

    # Build schema-compliant execution_authorization document
    auth_id = f"AUTH-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    auth_doc: Dict[str, Any] = {
        "schema_version": DEC_022_SCHEMA_VERSION,
        "authorization_id": auth_id,
        "project_id": project_id,
        "task_id": task_id,
        "gate": gate,
        "risk_class": risk_class if risk_class in ("R0", "R1", "R2", "R3", "R4") else "R1",
        "decision": decision,
        "authority_source": authority_source,
        "reason_codes": reason_codes,
        "human_critical_categories": sorted(list(detected_critical)),
        "forbidden_categories": sorted(list(detected_forbidden)),
        "execution_base_sha": base_sha,
        "control_source_sha": context.get("control_source_sha"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    # Validate auth_doc against schema
    val = validate_document("execution_authorization", auth_doc)
    if not val.is_valid:
        errs = "; ".join(str(e) for e in val.errors)
        raise ValueError(f"Generated execution_authorization invalid: {errs}")

    return HumanGateDecisionResult(
        decision=decision,
        authority_source=authority_source,
        reason_codes=reason_codes,
        human_critical_categories=sorted(list(detected_critical)),
        forbidden_categories=sorted(list(detected_forbidden)),
        execution_authorization_doc=auth_doc,
    )
