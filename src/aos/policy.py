"""Deterministic policy engine for AOS Shadow Orchestrator."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple
from aos.validate import validate_document

class PolicyCheckResult:
    def __init__(self, check_id: str, status: str, message: str):
        self.check_id = check_id
        self.status = status  # PASS or FAIL
        self.message = message

    def to_dict(self) -> Dict[str, str]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "message": self.message
        }

class PolicyEngine:
    """Enforces deterministic policy checks on planner decisions."""

    def evaluate(
        self,
        decision: Dict[str, Any],
        expected_project_id: str,
        pinned_source_sha: str,
        canonical_state_data: Dict[str, Any],
        re_resolved_sha: str | None = None
    ) -> Tuple[List[PolicyCheckResult], str]:
        checks: List[PolicyCheckResult] = []

        # Check 1: Schema validity
        val_res = validate_document("planner_decision", decision)
        if not val_res.is_valid:
            err_msg = "; ".join([e.message for e in val_res.errors])
            checks.append(PolicyCheckResult("SCHEMA_VALIDATION", "FAIL", f"Planner decision schema invalid: {err_msg}"))
        else:
            checks.append(PolicyCheckResult("SCHEMA_VALIDATION", "PASS", "Planner decision matches schema"))

        # Check 2: Project ID match
        dec_project_id = decision.get("project_id")
        if dec_project_id != expected_project_id:
            checks.append(PolicyCheckResult("PROJECT_ID_MATCH", "FAIL", f"Project ID '{dec_project_id}' != expected '{expected_project_id}'"))
        else:
            checks.append(PolicyCheckResult("PROJECT_ID_MATCH", "PASS", f"Project ID matches '{expected_project_id}'"))

        # Check 3: Source SHA match
        dec_source_sha = decision.get("source_sha")
        if dec_source_sha != pinned_source_sha:
            checks.append(PolicyCheckResult("SOURCE_SHA_MATCH", "FAIL", f"Decision source SHA '{dec_source_sha}' != pinned SHA '{pinned_source_sha}'"))
        else:
            checks.append(PolicyCheckResult("SOURCE_SHA_MATCH", "PASS", f"Source SHA matches pinned SHA '{pinned_source_sha}'"))

        # Check 4: Stale revision defense (if re-resolved SHA provided)
        if re_resolved_sha is not None:
            if re_resolved_sha != pinned_source_sha:
                checks.append(PolicyCheckResult("STALE_REVISION_DEFENSE", "FAIL", f"Source ref moved during batch: '{re_resolved_sha}' != '{pinned_source_sha}'"))
            else:
                checks.append(PolicyCheckResult("STALE_REVISION_DEFENSE", "PASS", "Source ref remained unchanged during decision batch"))

        # Check 5: Mutation intent must be NONE
        mutation_intent = decision.get("mutation_intent")
        if mutation_intent != "NONE":
            checks.append(PolicyCheckResult("MUTATION_INTENT", "FAIL", f"Mutation intent '{mutation_intent}' != 'NONE'"))
        else:
            checks.append(PolicyCheckResult("MUTATION_INTENT", "PASS", "Mutation intent is NONE"))

        # Check 6: Risk class must be R0
        risk_class = decision.get("risk_class")
        if risk_class != "R0":
            checks.append(PolicyCheckResult("RISK_CLASS", "FAIL", f"Risk class '{risk_class}' != 'R0'"))
        else:
            checks.append(PolicyCheckResult("RISK_CLASS", "PASS", "Risk class is R0"))

        # Check 7: Canonical milestone exact match
        canonical_milestone = canonical_state_data.get("current_milestone")
        dec_milestone = decision.get("selected_milestone")
        if dec_milestone != canonical_milestone:
            checks.append(PolicyCheckResult("CANONICAL_MILESTONE_MATCH", "FAIL", f"Selected milestone '{dec_milestone}' != canonical '{canonical_milestone}'"))
        else:
            checks.append(PolicyCheckResult("CANONICAL_MILESTONE_MATCH", "PASS", f"Selected milestone matches canonical '{canonical_milestone}'"))

        # Check 8: Canonical next_action exact match
        canonical_next_action = canonical_state_data.get("next_action")
        dec_next_action = decision.get("selected_next_action")
        if dec_next_action != canonical_next_action:
            checks.append(PolicyCheckResult("CANONICAL_NEXT_ACTION_MATCH", "FAIL", f"Selected next_action does not match canonical next_action"))
        else:
            checks.append(PolicyCheckResult("CANONICAL_NEXT_ACTION_MATCH", "PASS", "Selected next_action matches canonical next_action"))

        # Check 9: Frozen Core RC SHA match (if present in LARI canonical state)
        lari_core_rc4_sha = canonical_state_data.get("canonical_refs", {}).get("core_rc4", {}).get("sha")
        if lari_core_rc4_sha:
            # Verify that next action references this exact frozen SHA if mentioned
            if "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a" in canonical_next_action and lari_core_rc4_sha != "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a":
                checks.append(PolicyCheckResult("FROZEN_BASELINE_MATCH", "FAIL", f"Frozen baseline SHA '{lari_core_rc4_sha}' != expected 'e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a'"))
            else:
                checks.append(PolicyCheckResult("FROZEN_BASELINE_MATCH", "PASS", "Frozen baseline SHA matches expected"))

        # Check 10: Ambiguity & Human Gate handling
        ambiguity_detected = decision.get("ambiguity_detected", False)
        human_gate_required = decision.get("human_gate_required", False)
        dec_disposition = decision.get("disposition")

        if (ambiguity_detected or human_gate_required) and dec_disposition != "HOLD":
            checks.append(PolicyCheckResult("AMBIGUITY_HOLD", "FAIL", f"Ambiguity/Human gate required but disposition is '{dec_disposition}'"))
        else:
            checks.append(PolicyCheckResult("AMBIGUITY_HOLD", "PASS", "Ambiguity/Human gate handling consistent with disposition"))

        # Final disposition decision
        all_passed = all(c.status == "PASS" for c in checks)
        final_disposition = "SHADOW_ACCEPT" if (all_passed and dec_disposition == "SHADOW_ACCEPT") else "HOLD"

        return checks, final_disposition
