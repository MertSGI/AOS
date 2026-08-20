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
        with pytest.raises(PermissionError, match="OPENAI_API_KEY environment variable is missing"):
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
        with pytest.raises(ConnectionError, match="OpenAI transient error"):
            provider.generate_plan("prompt", {})

        # Mock AuthenticationError
        def mock_auth_error(*args, **kwargs):
            raise openai.AuthenticationError(message="Invalid API Key", response=mock_resp, body=None)

        monkeypatch.setattr("openai.resources.responses.Responses.create", mock_auth_error)
        with pytest.raises(PermissionError, match="OpenAI auth/permission failure"):
            provider.generate_plan("prompt", {})

class TestShadowOrchestrationNegativeMatrixOffline:
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
        # Expectation expects old Package/Customer Customization, but live state is LARİ Clinic
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

        adapter = FakeProjectSourceAdapter()  # returns LARİ Clinic
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
        assert provider.attempts == 0  # Zero planner calls made!

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
        assert any(c["check_id"] == "PROJECT_ID_MATCH" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

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
        assert any(c["check_id"] == "TARGET_BASE_SHA_MATCH" and c["status"] == "FAIL" for c in traces[0]["policy_checks"])

    def test_target_action_inconsistency_causes_hold(self, tmp_path):
        # Next action references different target base than state.json target_base_sha pointer
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
