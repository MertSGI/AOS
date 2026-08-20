"""Tests for AOS-2 provider routing, control ingress contract, and cross-provider benchmark."""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aos.planner import (
    FakePlannerProvider,
    PlannerContractError,
    PlannerCredentialError,
    PlannerTransientError,
)
from aos.provider_registry import (
    ProviderRegistry,
    ProviderRouter,
    RoutingResult,
    load_routing_policy,
)
from aos.providers.gemini import GeminiPlannerProvider
from aos.providers.groq import GroqPlannerProvider
from aos.providers.ollama import OllamaPlannerProvider
from aos.validate import validate_document, validate_file
from aos.shadow import run_shadow_orchestration
from aos.benchmark import run_benchmark

DESCRIPTOR_PATH = Path(__file__).parent.parent / "descriptors" / "lari.descriptor.json"
EXPECTATION_PATH = Path(__file__).parent.parent / "descriptors" / "lari.shadow-expectation.json"
POLICY_PATH = Path(__file__).parent.parent / "descriptors" / "lari.planner-policy.json"
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_policy(overrides: dict | None = None) -> dict:
    """Build a minimal routing policy dict with optional overrides."""
    base = {
        "schema_version": "0.1.0",
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {
            "R0": {"preferred_providers": ["gemini", "groq", "ollama"]}
        },
        "providers": {
            "gemini": {
                "provider_id": "gemini",
                "model_id": "gemini-3.6-flash",
                "credential_env_var": "GEMINI_API_KEY",
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "groq": {
                "provider_id": "groq",
                "model_id": "openai/gpt-oss-120b",
                "credential_env_var": "GROQ_API_KEY",
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "ollama": {
                "provider_id": "ollama",
                "model_id": "llama3.3:70b",
                "credential_env_var": None,
                "billing_class": "LOCAL",
                "structured_output": True,
                "cloud_local": "LOCAL",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC", "INTERNAL", "CONFIDENTIAL"],
            },
            "openai": {
                "provider_id": "openai",
                "model_id": "gpt-5.6-sol",
                "credential_env_var": "OPENAI_API_KEY",
                "billing_class": "PAID",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC", "INTERNAL"],
            },
        },
    }
    if overrides:
        base.update(overrides)
    return base


class FakeProjectSourceAdapter:
    """Offline fake source adapter for deterministic tests."""
    def __init__(self, sha="4c55eecdbe064c74b34af31a1daf9851689e4fe8", state_data=None):
        self.repository = "MertSGI/Randapp-main"
        self.control_ref = "control/lari-project-control-plane"
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
            },
        }

    def resolve_ref_to_sha(self):
        return self.fake_sha

    def fetch_file_at_sha(self, path, exact_sha):
        if "STATE.json" in path:
            return json.dumps(self.state_data)
        return f"# Fake content for {path} at {exact_sha}"

    def fetch_canonical_context(self, exact_sha, paths):
        import hashlib
        contents = {}
        hashes = {}
        for key, path in paths.items():
            content = self.fetch_file_at_sha(path, exact_sha)
            contents[key] = content
            hashes[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return contents, hashes

    def build_normalized_snapshot(self, project_id, exact_sha, raw_contents, file_hashes, projection_config=None):
        from aos.source_adapter import resolve_json_pointer
        state_raw = raw_contents.get("state")
        state_data = json.loads(state_raw)
        p_config = projection_config or {}
        status_ptr = p_config.get("current_status_pointer", "/current_status")
        milestone_ptr = p_config.get("current_milestone_pointer", "/current_milestone")
        next_action_ptr = p_config.get("canonical_next_action_pointer", "/next_action")
        target_sha_ptr = p_config.get("target_base_sha_pointer")
        target_sha_required = p_config.get("target_base_sha_required", False)

        current_status = resolve_json_pointer(state_data, status_ptr) or resolve_json_pointer(state_data, "/status")
        current_milestone = resolve_json_pointer(state_data, milestone_ptr)
        canonical_next_action = resolve_json_pointer(state_data, next_action_ptr)
        target_base_sha = resolve_json_pointer(state_data, target_sha_ptr) if target_sha_ptr else None

        ambiguity_reasons = []
        if not current_milestone:
            ambiguity_reasons.append(f"Missing milestone")
        if not canonical_next_action:
            ambiguity_reasons.append(f"Missing next action")
        if target_sha_required and not target_base_sha:
            ambiguity_reasons.append(f"Missing target base SHA")
        if p_config.get("require_target_base_in_next_action") and target_base_sha and canonical_next_action:
            if target_base_sha not in str(canonical_next_action):
                ambiguity_reasons.append(f"Next action missing target base SHA")

        return {
            "schema_version": "0.1.0",
            "project_id": project_id,
            "repository": self.repository,
            "source_ref": self.control_ref,
            "source_sha": exact_sha,
            "current_status": str(current_status) if current_status else None,
            "current_milestone": str(current_milestone) if current_milestone else "UNKNOWN_MILESTONE",
            "canonical_next_action": str(canonical_next_action) if canonical_next_action else "UNKNOWN_NEXT_ACTION",
            "target_base_sha": str(target_base_sha) if target_base_sha else None,
            "has_ambiguity": len(ambiguity_reasons) > 0,
            "ambiguity_reasons": ambiguity_reasons,
            "input_file_hashes": file_hashes,
        }


# =========================================================================
# Provider Router Tests
# =========================================================================

class TestProviderRouterDeterministic:
    def test_free_first_selection(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        policy = _make_policy()
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0")
        assert result is not None
        assert result.selected_provider_id == "gemini"
        assert result.selected_model_id == "gemini-3.6-flash"
        assert result.fallback_used is False

    def test_paid_fallback_disabled(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        policy = _make_policy({"allow_paid_fallback": False})
        # Disable local ollama too
        policy["providers"]["ollama"]["enabled"] = False
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0")
        assert result is None  # PROVIDER_POLICY_HOLD

    def test_data_classification_filtering(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        policy = _make_policy({"data_classification": "CONFIDENTIAL"})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0")
        # Gemini/Groq only allow PUBLIC, ollama allows CONFIDENTIAL
        assert result is not None
        assert result.selected_provider_id == "ollama"

    def test_credential_missing_skips_to_next_free(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        policy = _make_policy()
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0")
        assert result is not None
        assert result.selected_provider_id == "groq"

    def test_no_allowed_provider_returns_none(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        policy = _make_policy({"allow_paid_fallback": False})
        policy["providers"]["ollama"]["enabled"] = False
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0")
        assert result is None

    def test_transient_provider_fallback_when_allowed(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        policy = _make_policy({"allow_provider_fallback": True})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        result = router.select(risk_class="R0", skip_providers=["gemini"])
        assert result is not None
        assert result.selected_provider_id == "groq"
        assert result.fallback_used is True


# =========================================================================
# Provider Adapter Mock Tests
# =========================================================================

class TestGeminiProviderMock:
    def test_gemini_missing_key_raises_credential_error(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        provider = GeminiPlannerProvider(model="gemini-3.6-flash")
        with pytest.raises(PlannerCredentialError, match="GEMINI_API_KEY"):
            provider.generate_plan("test prompt", {})

    def test_gemini_structured_output_mock(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        provider = GeminiPlannerProvider(model="gemini-3.6-flash")

        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "abc123",
            "selected_milestone": "Test",
            "selected_next_action": "Test action",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mock",
            "disposition": "SHADOW_ACCEPT",
        })
        mock_response.candidates = []
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.cached_content_token_count = 0
        mock_response.usage_metadata.candidates_token_count = 50
        mock_response.usage_metadata.total_token_count = 150

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            decision, resp_id, usage = provider.generate_plan("test", {})

        assert decision["project_id"] == "lari"
        assert usage["input_tokens"] == 100
        assert usage["total_tokens"] == 150


class TestGroqProviderMock:
    def test_groq_missing_key_raises_credential_error(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        provider = GroqPlannerProvider()
        with pytest.raises(PlannerCredentialError, match="GROQ_API_KEY"):
            provider.generate_plan("test prompt", {})

    def test_groq_structured_output_mock(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        provider = GroqPlannerProvider()

        decision_json = json.dumps({
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "abc123",
            "selected_milestone": "Test",
            "selected_next_action": "Test action",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mock",
            "disposition": "SHADOW_ACCEPT",
        })

        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = decision_json
        mock_choice.message.refusal = None

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.id = "groq-resp-001"
        mock_response.usage.prompt_tokens = 80
        mock_response.usage.completion_tokens = 40
        mock_response.usage.total_tokens = 120

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_openai_client):
            decision, resp_id, usage = provider.generate_plan("test", {})

        assert decision["project_id"] == "lari"
        assert resp_id == "groq-resp-001"
        assert usage["total_tokens"] == 120


class TestOllamaProviderMock:
    def test_ollama_structured_output_mock(self, monkeypatch):
        provider = OllamaPlannerProvider(model="llama3.3:70b")

        decision_json = json.dumps({
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "abc123",
            "selected_milestone": "Test",
            "selected_next_action": "Test action",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mock",
            "disposition": "SHADOW_ACCEPT",
        })

        mock_ollama_response = json.dumps({
            "message": {"content": decision_json},
            "prompt_eval_count": 90,
            "eval_count": 35,
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_ollama_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("aos.providers.ollama.urllib.request.urlopen", return_value=mock_resp):
            decision, resp_id, usage = provider.generate_plan("test", {})

        assert decision["project_id"] == "lari"
        assert usage["input_tokens"] == 90
        assert usage["output_tokens"] == 35

    def test_ollama_connection_error_raises_transient(self):
        import urllib.error
        provider = OllamaPlannerProvider(model="llama3.3:70b")
        with patch("aos.providers.ollama.urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(PlannerTransientError, match="Ollama connection error"):
                provider.generate_plan("test", {})


# =========================================================================
# Common Schema Across Providers
# =========================================================================

class TestCommonPlannerSchema:
    def test_planner_schema_accepted_across_all_providers(self):
        """Verify all providers use the same planner_decision schema contract."""
        from aos.validate import load_schema
        schema = load_schema("planner_decision.schema.json")
        decision = {
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Test action",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Test rationale",
            "disposition": "SHADOW_ACCEPT",
        }
        res = validate_document("planner_decision", decision)
        assert res.is_valid is True


# =========================================================================
# Trace Metadata Tests
# =========================================================================

class TestTraceProviderMetadata:
    def test_trace_with_provider_metadata_valid(self):
        trace = {
            "schema_version": "0.1.0",
            "trace_id": "TRACE-TEST-001",
            "timestamp": "2026-08-20T19:00:00Z",
            "project_id": "lari",
            "repository": "MertSGI/Randapp-main",
            "configured_source_ref": "control/lari-project-control-plane",
            "resolved_source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "input_file_hashes": {},
            "planner_provider": "GeminiPlannerProvider",
            "model": "gemini-3.6-flash",
            "provider_response_id": None,
            "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50, "total_tokens": 150},
            "planner_decision": {"project_id": "lari"},
            "policy_checks": [{"check_id": "TEST", "status": "PASS", "message": "ok"}],
            "final_disposition": "SHADOW_ACCEPT",
            "mutation_performed": False,
            "limitations": ["Shadow mode only."],
            "provider_id": "gemini",
            "billing_class": "FREE_TIER",
            "data_classification": "PUBLIC",
            "selection_reason": "Free-tier Gemini selected for R0 PUBLIC",
            "fallback_used": False,
        }
        res = validate_document("shadow_trace", trace)
        assert res.is_valid is True

    def test_no_credential_in_trace(self):
        """Ensure no credential-like content appears in trace structure."""
        trace_str = json.dumps({
            "schema_version": "0.1.0",
            "trace_id": "TRACE-SEC-001",
            "planner_provider": "GeminiPlannerProvider",
            "model": "gemini-3.6-flash",
        })
        assert "API_KEY" not in trace_str
        assert "sk-" not in trace_str
        assert "AIza" not in trace_str


# =========================================================================
# Control Request Contract Tests
# =========================================================================

class TestControlRequestContract:
    def test_valid_control_request(self):
        path = FIXTURES_DIR / "valid" / "control_request.valid.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is True

    def test_force_pass_evidence_rejected(self):
        path = FIXTURES_DIR / "invalid" / "control_request.force_pass.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is False
        assert any("FORCE_PASS_EVIDENCE" in str(e.message) for e in res.errors)

    def test_missing_request_type_rejected(self):
        path = FIXTURES_DIR / "invalid" / "control_request.missing_type.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is False

    def test_no_force_pass_evidence_in_enum(self):
        from aos.validate import load_schema
        schema = load_schema("control_request.schema.json")
        allowed_types = schema["properties"]["request_type"]["enum"]
        assert "FORCE_PASS_EVIDENCE" not in allowed_types


# =========================================================================
# Routing Policy Schema Tests
# =========================================================================

class TestRoutingPolicySchema:
    def test_lari_routing_policy_valid(self):
        res, code = validate_file("planner_routing_policy", str(POLICY_PATH))
        assert code == 0
        assert res.is_valid is True

    def test_routing_policy_load(self):
        reg = load_routing_policy(str(POLICY_PATH))
        assert reg.allow_paid_fallback is False
        assert reg.data_classification == "PUBLIC"
        gemini = reg.get_provider("gemini")
        assert gemini is not None
        assert gemini.billing_class == "FREE_TIER"


# =========================================================================
# Stale Expectation Proof
# =========================================================================

class TestStaleLariExpectation:
    def test_stale_expectation_prevents_all_provider_calls(self, tmp_path):
        """Current committed expectation is stale — LARI has moved. Zero planner calls must occur."""
        adapter = FakeProjectSourceAdapter(
            sha="98da4887c53b35d099d7e4aa07cf9c03c87035e9",
            state_data={
                "schema_version": "1.0.0",
                "current_status": "CORE_SOFTWARE_RC_CLOSED_PROVEN",
                "current_milestone": "LARİ Clinic",
                "next_action": "Execute Controller-authorized LARİ Clinic Block 2 Operational Integration.",
                "canonical_refs": {
                    "package_customization_baseline": {
                        "sha": "65a53427f52c21e60aa8f92e02a17d693a201601"
                    }
                },
            },
        )
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
        )
        assert disp == "STALE_EXPECTATION"
        assert code == 1
        assert len(traces) == 0
        assert provider.attempts == 0


# =========================================================================
# Benchmark Source SHA Movement
# =========================================================================

class TestBenchmarkSourceMovement:
    def test_benchmark_sha_movement_invalidates_batch(self, tmp_path):
        """If source SHA moves during benchmark, entire benchmark is STALE."""
        stale_adapter = FakeProjectSourceAdapter(
            sha="ffffffffffffffffffffffffffffffffffffffff",
        )
        provider = FakePlannerProvider()
        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini"],
            repeat=3,
            trace_dir_override=tmp_path,
            provider_overrides={"gemini": provider},
        )
        assert results["benchmark_status"] == "STALE"


# =========================================================================
# Semantic Failure Provider Shopping Prevention
# =========================================================================

class TestSemanticFailureNoProviderShopping:
    def test_semantic_failure_does_not_provider_shop(self, tmp_path):
        """A semantic HOLD result must not cause retrying with a different provider for a better answer."""
        bad_decision = {
            "schema_version": "0.1.0",
            "project_id": "WRONG_ID",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Wrong project",
            "disposition": "SHADOW_ACCEPT",
        }
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider(decision_override=bad_decision)
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
        )
        assert disp == "HOLD"
        assert provider.attempts == 1  # No additional calls to shop for PASS
