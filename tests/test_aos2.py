"""Unit and integration tests for AOS-2 Shadow Orchestrator."""

import json
import pytest
from pathlib import Path
from aos.source_adapter import ProjectSourceAdapter
from aos.planner import (
    FakePlannerProvider,
    OpenAIPlannerProvider,
    PlannerContractError,
    PlannerCredentialError,
    PlannerTransientError,
)
from aos.policy import PolicyEngine
from aos.shadow import run_shadow_orchestration
from aos.validate import validate_document

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"
EXPECTATION_PATH = Path(__file__).parent.parent / "descriptors" / "lari.shadow-expectation.json"

class FakeProjectSourceAdapter(ProjectSourceAdapter):
    """Local, offline fake source adapter for deterministic unit tests."""
    def __init__(self, sha: str = "4c55eecdbe064c74b34af31a1daf9851689e4fe8", state_data: dict | None = None):
        super().__init__("MertSGI/Randapp-main", "control/lari-project-control-plane")
        self.fake_sha = sha
        self.state_data = state_data or {
            "schema_version": "1.0.0",
            "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
            "current_milestone": "LARİ Clinic",
            "next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "canonical_refs": {
                "package_customization_baseline": {
                    "sha": "65a53427f52c21e60aa8f92e02a17d693a201601"
                }
            }
        }

    def resolve_ref_to_sha(self) -> str:
        return self.fake_sha

    def fetch_file_at_sha(self, path: str, exact_sha: str) -> str:
        if "STATE.json" in path:
            return json.dumps(self.state_data)
        return f"# Fake content for {path} at {exact_sha}"

class TestSourceAdapterUnit:
    def test_source_adapter_exposes_no_mutation_methods(self):
        adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/lari-project-control-plane")
        mutation_keywords = ["post", "put", "patch", "delete", "write", "commit", "push", "create_pr"]
        methods = [m for m in dir(adapter) if not m.startswith("_")]
        for m in methods:
            for kw in mutation_keywords:
                assert kw not in m.lower(), f"Source adapter method '{m}' contains mutation keyword '{kw}'"

    @pytest.mark.live_read_only
    def test_live_read_only_source_adapter_smoke(self):
        adapter = ProjectSourceAdapter("MertSGI/Randapp-main", "control/lari-project-control-plane")
        sha = adapter.resolve_ref_to_sha()
        assert len(sha) == 40
        content = adapter.fetch_file_at_sha("docs/project-control/STATE.json", sha)
        assert "current_milestone" in content or "status" in content

class TestOpenAIPlannerProviderUnit:
    def test_openai_provider_instantiation_without_api_call(self):
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        assert provider.model == "gpt-5.6-sol"

    def test_openai_provider_missing_key_raises_permission_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")
        with pytest.raises(PlannerCredentialError, match="OPENAI_API_KEY environment variable is missing"):
            provider.generate_plan("dummy prompt", {})

    def test_openai_error_taxonomy_mapping(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")

        import openai
        import httpx2
        mock_req = httpx2.Request("POST", "https://api.openai.com/v1/responses")
        mock_resp = httpx2.Response(401, request=mock_req)

        # Mock APIConnectionError
        def mock_conn_error(*args, **kwargs):
            raise openai.APIConnectionError(request=mock_req)

        monkeypatch.setattr("openai.resources.responses.Responses.create", mock_conn_error)
        with pytest.raises(PlannerTransientError, match="OpenAI transient error"):
            provider.generate_plan("prompt", {})

        # Mock AuthenticationError
        def mock_auth_error(*args, **kwargs):
            raise openai.AuthenticationError(message="Invalid API Key", response=mock_resp, body=None)

        monkeypatch.setattr("openai.resources.responses.Responses.create", mock_auth_error)
        with pytest.raises(PlannerCredentialError, match="OpenAI auth/permission failure"):
            provider.generate_plan("prompt", {})

        # Mock BadRequestError
        def mock_bad_req(*args, **kwargs):
            mock_400 = httpx2.Response(400, request=mock_req)
            raise openai.BadRequestError(message="Bad Request", response=mock_400, body=None)

        monkeypatch.setattr("openai.resources.responses.Responses.create", mock_bad_req)
        with pytest.raises(PlannerContractError, match="OpenAI invalid request/schema"):
            provider.generate_plan("prompt", {})

class TestShadowOrchestrationFullRegressionOffline:
    def test_valid_shadow_orchestration_passes_offline(self, tmp_path):
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter
        )
        assert code == 0
        assert disp == "SHADOW_ACCEPT"
        assert len(traces) == 1
        assert traces[0]["final_disposition"] == "SHADOW_ACCEPT"
        assert traces[0]["mutation_performed"] is False

    def test_shadow_expectation_valid_passes(self, tmp_path):
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter
        )
        assert code == 0
        assert disp == "SHADOW_ACCEPT"

    def test_stale_expectation_triggers_hold_and_zero_planner_calls(self, tmp_path):
        old_expectation_file = tmp_path / "old_expectation.json"
        old_expectation_file.write_text(json.dumps({
            "schema_version": "0.1.0",
            "project_id": "lari",
            "expected_source_ref": "control/lari-project-control-plane",
            "expected_source_sha": "262f7ed87d71419ec469234d4b611c2556069f2d",
            "expected_milestone": "Package/Customer Customization",
            "expected_canonical_next_action": "Audit Core extension points and define package/customer customization contract against frozen Core RC baseline (e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a)",
            "expected_target_base_sha": "e1bb23dbbc2f1f079ec6bbc93e3cb9b83db1839a",
            "created_at": "2026-08-20T00:00:00Z",
            "verifier_identity": "test-verifier"
        }))

        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(old_expectation_file),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter
        )
        assert code == 1
        assert disp == "STALE_EXPECTATION"
        assert len(traces) == 0
        assert provider.attempts == 0

    def test_wrong_project_id_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "WRONG_PROJECT_ID",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong project ID test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_wrong_source_sha_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "0000000000000000000000000000000000000000",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong source SHA test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_invented_milestone_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "INVENTED_FUTURE_MILESTONE",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Invented milestone test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_invented_next_action_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "INVENTED_UNAPPROVED_NEXT_ACTION",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Invented next action test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_wrong_target_base_sha_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "0000000000000000000000000000000000000000",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong target base test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_missing_required_target_base_sha_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Missing required target base test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_target_action_inconsistency_causes_hold(self, tmp_path):
        bad_state = {
            "schema_version": "1.0.0",
            "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
            "current_milestone": "LARİ Clinic",
            "next_action": "Controller-authorized LARİ Clinic foundation materialization from frozen Package baseline 0000000000000000000000000000000000000000.",
            "canonical_refs": {
                "package_customization_baseline": {
                    "sha": "65a53427f52c21e60aa8f92e02a17d693a201601"
                }
            }
        }
        adapter = FakeProjectSourceAdapter(state_data=bad_state)
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_mutation_intent_not_none_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "ISOLATED_MUTATION",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mutation intent test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_risk_class_not_r0_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R1",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Risk class test",
            "disposition": "SHADOW_ACCEPT"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_planner_ambiguity_detected_causes_hold(self, tmp_path):
        override = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": True,
            "ambiguity_reasons": ["Contradictory requirement detected"],
            "human_gate_required": True,
            "rationale": "Ambiguity detected in input",
            "disposition": "HOLD"
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=override)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_canonical_ambiguity_causes_hold(self, tmp_path):
        ambiguous_state = {
            "schema_version": "1.0.0",
            "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
            "current_milestone": "",  # missing milestone
            "next_action": "Some next action",
        }
        adapter = FakeProjectSourceAdapter(state_data=ambiguous_state)
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_source_re_resolution_failure_causes_hold(self, monkeypatch, tmp_path):
        adapter = FakeProjectSourceAdapter()
        count = 0
        def mock_resolve():
            nonlocal count
            count += 1
            if count > 1:
                raise RuntimeError("Re-resolution network failure")
            return "4c55eecdbe064c74b34af31a1daf9851689e4fe8"

        adapter.resolve_ref_to_sha = mock_resolve
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_source_branch_movement_causes_hold(self, tmp_path):
        adapter = FakeProjectSourceAdapter()
        count = 0
        def mock_resolve():
            nonlocal count
            count += 1
            if count > 1:
                return "ffffffffffffffffffffffffffffffffffffffff"
            return "4c55eecdbe064c74b34af31a1daf9851689e4fe8"

        adapter.resolve_ref_to_sha = mock_resolve
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"

    def test_planner_contract_error_causes_hold_disposition(self, tmp_path):
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(exception_to_raise=PlannerContractError("Model output invalid JSON"))
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider, trace_dir_override=tmp_path, adapter_override=adapter
        )
        assert code != 0
        assert disp == "HOLD"
        assert provider.attempts == 1

    def test_transient_provider_failure_retry_and_overflow(self, tmp_path):
        adapter = FakeProjectSourceAdapter()
        # 1 transient failure succeeds on retry
        provider1 = FakePlannerProvider(transient_failures_count=1)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider1, trace_dir_override=tmp_path, adapter_override=adapter, max_network_retries=1
        )
        assert code == 0
        assert disp == "SHADOW_ACCEPT"
        assert provider1.attempts == 2

        # 2 transient failures overflow max 1 retry
        provider2 = FakePlannerProvider(transient_failures_count=2)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH), repeat=1, provider_override=provider2, trace_dir_override=tmp_path, adapter_override=adapter, max_network_retries=1
        )
        assert code != 0
        assert disp == "PROVIDER_UNAVAILABLE"

    def test_missing_descriptor_control_ref_rejected(self):
        path = Path(__file__).parent / "fixtures" / "invalid" / "project_descriptor.missing_control_ref.json"
        res = validate_document("project_descriptor", json.loads(path.read_text(encoding="utf-8")))
        assert res.is_valid is False
