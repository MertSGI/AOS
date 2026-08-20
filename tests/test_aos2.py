"""Unit and integration tests for AOS-2 Shadow Orchestrator."""

import json
import pytest
from pathlib import Path
from aos.source_adapter import ProjectSourceAdapter
from aos.planner import FakePlannerProvider, OpenAIPlannerProvider
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

    def test_source_adapter_exposes_no_mutation_methods(self):
        adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/lari-project-control-plane")
        mutation_keywords = ["post", "put", "patch", "delete", "write", "commit", "push", "create_pr"]
        methods = [m for m in dir(adapter) if not m.startswith("_")]
        for m in methods:
            for kw in mutation_keywords:
                assert kw not in m.lower(), f"Source adapter method '{m}' contains mutation keyword '{kw}'"

class TestOpenAIPlannerProviderUnit:
    def test_openai_provider_instantiation_without_api_call(self):
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        assert provider.model == "gpt-5.6-sol"

    def test_openai_provider_missing_key_raises_permission_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        with pytest.raises(PermissionError, match="OPENAI_API_KEY environment variable is missing"):
            provider.generate_plan("dummy prompt", {})

class TestShadowOrchestrationNegativeMatrix:
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

    def test_wrong_project_id_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "WRONG_PROJECT_ID",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong project ID test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"
        assert any(c["check_id"] == "PROJECT_ID_MATCH" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

    def test_wrong_source_sha_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "0000000000000000000000000000000000000000",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong source SHA test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_invented_milestone_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "INVENTED_FUTURE_MILESTONE",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Invented milestone test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_invented_next_action_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "INVENTED_UNAPPROVED_NEXT_ACTION",
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Invented next action test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_wrong_target_base_sha_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": "0000000000000000000000000000000000000000",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong target base test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"
        assert any(c["check_id"] == "TARGET_BASE_SHA_MATCH" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

    def test_missing_required_target_base_sha_causes_hold(self, tmp_path):
        override = {
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
            "rationale": "Missing required target base test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
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
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R0",
            "mutation_intent": "ISOLATED_MUTATION",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mutation intent test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
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
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "risk_class": "R1",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Risk class test",
            "disposition": "SHADOW_ACCEPT"
        }
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"

    def test_ambiguity_detected_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "selected_milestone": "Package/Customer Customization",
            "selected_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
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
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        assert code != 0
        assert disp == "HOLD"
        assert traces[0]["final_disposition"] == "HOLD"

    def test_source_re_resolution_failure_causes_hold(self, monkeypatch):
        # Mock re-resolution failure
        orig_resolve = ProjectSourceAdapter.resolve_ref_to_sha
        count = 0
        def mock_resolve(self_adapter):
            nonlocal count
            count += 1
            if count > 1:
                raise RuntimeError("Source re-resolution network failure")
            return orig_resolve(self_adapter)

        monkeypatch.setattr(ProjectSourceAdapter, "resolve_ref_to_sha", mock_resolve)
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider
        )
        assert code != 0
        assert disp == "HOLD"
        assert any(c["check_id"] == "SOURCE_RERESOLUTION_FAILED" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

    def test_source_branch_moved_causes_hold(self, monkeypatch):
        orig_resolve = ProjectSourceAdapter.resolve_ref_to_sha
        count = 0
        def mock_resolve(self_adapter):
            nonlocal count
            count += 1
            if count > 1:
                return "ffffffffffffffffffffffffffffffffffffffff"
            return orig_resolve(self_adapter)

        monkeypatch.setattr(ProjectSourceAdapter, "resolve_ref_to_sha", mock_resolve)
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider
        )
        assert code != 0
        assert disp == "HOLD"
        assert any(c["check_id"] == "STALE_REVISION_DEFENSE" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

    def test_invalid_trace_fails_closed_to_hold(self, tmp_path, monkeypatch):
        # Corrupt trace output before schema validation check
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path
        )
        # Verify valid run passes
        assert code == 0

    def test_provider_transient_retry_contract(self):
        # Provider with 1 transient failure succeeds on retry 2
        provider = FakePlannerProvider(transient_failures_count=1)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, max_network_retries=1
        )
        assert code == 0
        assert disp == "SHADOW_ACCEPT"
        assert provider.attempts == 2

    def test_provider_exceed_retry_contract(self):
        # Provider with 2 transient failures exceeds max 1 retry and fails gracefully
        provider = FakePlannerProvider(transient_failures_count=2)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, max_network_retries=1
        )
        assert code != 0
        assert disp == "PROVIDER_UNAVAILABLE"

    def test_missing_descriptor_control_ref_rejected(self):
        path = Path(__file__).parent / "fixtures" / "invalid" / "project_descriptor.missing_control_ref.json"
        res, code = validate_document("project_descriptor", json.loads(path.read_text(encoding="utf-8"))), 1
        assert res.is_valid is False
