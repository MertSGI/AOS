"""Unit and integration tests for AOS-2 Shadow Orchestrator."""

import json
import pytest
from pathlib import Path
from aos.source_adapter import ProjectSourceAdapter
from aos.planner import FakePlannerProvider
from aos.policy import PolicyEngine
from aos.shadow import run_shadow_orchestration
from aos.validate import validate_document

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"

class TestSourceAdapter:
    def test_read_only_source_adapter(self):
        adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/lari-project-control-plane")
        sha = adapter.resolve_ref_to_sha()
        assert len(sha) == 40
        content = adapter.fetch_file_at_sha("docs/project-control/STATE.json", sha)
        assert "current_milestone" in content or "status" in content

class TestShadowOrchestrationWithFakeProvider:
    def test_valid_shadow_orchestration_passes(self, tmp_path):
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code == 0
        assert disp == "SHADOW_ACCEPT"
        assert len(traces) == 1
        assert traces[0]["final_disposition"] == "SHADOW_ACCEPT"
        assert traces[0]["mutation_performed"] is False

    def test_invented_milestone_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "INVENTED_FUTURE_MILESTONE",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Testing invented milestone",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"
        assert traces[0]["final_disposition"] == "HOLD"

    def test_invented_next_action_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "INVENTED_UNAPPROVED_NEXT_ACTION",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Testing invented next action",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_mutation_intent_not_none_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "ISOLATED_MUTATION",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Testing mutation intent",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_risk_class_not_r0_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": None,
            "risk_class": "R1",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Testing risk class",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_ambiguity_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": True,
            "ambiguity_reasons": ["Contradictory requirement detected"],
            "human_gate_required": True,
            "rationale": "Ambiguity detected in input",
            "disposition": "HOLD"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"
        assert traces[0]["final_disposition"] == "HOLD"

    def test_stale_revision_causes_hold(self):
        engine = PolicyEngine()
        decision = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Testing stale revision",
            "disposition": "SHADOW_ACCEPT"
        }
        canonical_state = {
            "current_milestone": "Package/Customer Customization",
            "next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)"
        }
        checks, disp = engine.evaluate(
            decision,
            "lari",
            "262f7ed87d71419ec469234d4b611c2556069f2d",
            canonical_state,
            re_resolved_sha="9999999999999999999999999999999999999999"  # Branch moved
        )
        assert disp == "HOLD"
        assert any(c.check_id == "STALE_REVISION_DEFENSE" and c.status == "FAIL" for c in checks)
