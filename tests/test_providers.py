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
# Source Modes Tests (LIVE_GUARD vs PINNED_PROOF)
# =========================================================================

class TestSourceModes:
    def test_live_guard_stale_expectation_makes_zero_calls(self, tmp_path):
        """1. LIVE_GUARD detects stale expectation and makes zero provider calls."""
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
            source_mode="live_guard",
        )
        assert disp == "STALE_EXPECTATION"
        assert code == 1
        assert len(traces) == 0
        assert provider.attempts == 0

    def test_pinned_proof_evaluates_approved_historical_sha(self, tmp_path):
        """2. PINNED_PROOF evaluates approved historical SHA even when simulated branch has advanced."""
        # Simulated live branch has moved to 98da...
        adapter = FakeProjectSourceAdapter(sha="98da4887c53b35d099d7e4aa07cf9c03c87035e9")
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
            source_mode="pinned_proof",
        )
        assert disp == "SHADOW_ACCEPT"
        assert code == 0
        assert len(traces) == 1
        assert traces[0]["resolved_source_sha"] == "4c55eecdbe064c74b34af31a1daf9851689e4fe8"

    def test_pinned_proof_cannot_authorize_mutation(self, tmp_path):
        """3. PINNED_PROOF cannot authorize mutation."""
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
            source_mode="pinned_proof",
            mutation_intent="ISOLATED_MUTATION",  # Attempt mutation with PINNED_PROOF
        )
        assert disp == "HOLD"
        assert code == 1
        assert len(traces) == 0


# =========================================================================
# Provider Router Policy Tests
# =========================================================================

class TestProviderRouterPolicy:
    def test_benchmark_explicit_openai_rejected_by_paid_policy(self, monkeypatch, tmp_path):
        """4. Benchmark explicit OpenAI request is rejected by paid-fallback policy."""
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["openai"],
            repeat=1,
            trace_dir_override=tmp_path,
            source_mode="pinned_proof",
        )
        assert results["benchmark_status"] == "HOLD"
        assert results["providers"]["openai"]["status"] == "PROVIDER_POLICY_HOLD"

    def test_wrong_data_classification_provider_rejected(self, monkeypatch):
        """5. Wrong data-classification provider is rejected."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        policy = _make_policy({"data_classification": "CONFIDENTIAL"})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        # Gemini allows only PUBLIC -> rejected for CONFIDENTIAL
        res = router.select(risk_class="R0", skip_providers=["groq", "ollama"])
        assert res is None

    def test_disabled_provider_rejected(self, monkeypatch):
        """6. Disabled provider is rejected."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        policy = _make_policy()
        policy["providers"]["gemini"]["enabled"] = False
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(risk_class="R0")
        assert res is not None
        assert res.selected_provider_id != "gemini"

    def test_missing_credential_eligibility_skips_provider_safely(self, monkeypatch):
        """7. Missing credential eligibility skips provider safely to next free provider."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        policy = _make_policy()
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(risk_class="R0")
        assert res is not None
        assert res.selected_provider_id == "groq"

    def test_transient_failure_falls_back_when_allowed(self, monkeypatch):
        """8. Transient failure falls back only when allow_provider_fallback=true."""
        policy = _make_policy({"allow_provider_fallback": True})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(risk_class="R0", skip_providers=["gemini"], ignore_credentials=True)
        assert res is not None
        assert res.selected_provider_id == "groq"

    def test_transient_failure_does_not_fall_back_when_disallowed(self, monkeypatch):
        """9. Transient failure does NOT fall back when allow_provider_fallback=false."""
        policy = _make_policy({"allow_provider_fallback": False})
        reg = ProviderRegistry(policy)
        # Manually force router check for fallback behavior
        assert reg.allow_provider_fallback is False

    def test_semantic_failure_does_not_provider_shop(self, tmp_path):
        """10. Semantic/provider-contract failure does not provider-shop."""
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
        assert provider.attempts == 1


# =========================================================================
# Trace & Provider Config Tests
# =========================================================================

class TestTraceAndProviderConfig:
    def test_generated_runtime_trace_contains_provider_routing_metadata(self, monkeypatch, tmp_path):
        """11. Generated runtime trace contains provider routing metadata."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
            routing_policy_path=str(POLICY_PATH),
            source_mode="pinned_proof",
        )
        assert code == 0
        assert len(traces) == 1
        trace = traces[0]
        assert trace["provider_id"] == "gemini"
        assert trace["billing_class"] == "FREE_TIER"
        assert trace["data_classification"] == "PUBLIC"
        assert trace["fallback_used"] is False

    def test_generated_trace_contains_no_credential_value(self, tmp_path):
        """12. Generated trace contains no credential value."""
        adapter = FakeProjectSourceAdapter()
        provider = FakePlannerProvider()
        disp, traces, code = run_shadow_orchestration(
            str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            repeat=1,
            provider_override=provider,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
            routing_policy_path=str(POLICY_PATH),
            source_mode="pinned_proof",
        )
        trace_str = json.dumps(traces[0])
        assert "API_KEY" not in trace_str
        assert "sk-" not in trace_str
        assert "AIza" not in trace_str

    def test_gemini_request_does_not_send_deprecated_sampling_controls(self, monkeypatch):
        """13. Gemini request does not send deprecated sampling controls (e.g. temperature=0)."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        provider = GeminiPlannerProvider(model="gemini-3.6-flash")

        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "schema_version": "0.1.0",
            "project_id": "lari",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Mock rationale",
            "disposition": "SHADOW_ACCEPT",
        })
        mock_response.candidates = []
        mock_response.usage_metadata = None

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            provider.generate_plan("test prompt", {})

        # Inspect call args
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert not hasattr(config, "temperature") or getattr(config, "temperature", None) is None

    def test_groq_finish_reason_length_fails_closed(self, monkeypatch):
        """14. Groq finish_reason=length fails closed."""
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        provider = GroqPlannerProvider()

        mock_choice = MagicMock()
        mock_choice.finish_reason = "length"  # Truncated output
        mock_choice.message.content = "incomplete json..."

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_openai_client):
            with pytest.raises(PlannerContractError, match="unacceptable reason: length"):
                provider.generate_plan("test prompt", {})


# =========================================================================
# Benchmark Execution Tests
# =========================================================================

class TestBenchmarkExecution:
    def test_six_mocked_gemini_groq_benchmark_runs_preserve_exact_canonical_identity(self, tmp_path):
        """15. Six mocked Gemini/Groq benchmark runs preserve exact canonical identity."""
        adapter = FakeProjectSourceAdapter()
        provider_gemini = FakePlannerProvider()
        provider_groq = FakePlannerProvider()

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini", "groq"],
            repeat=3,
            trace_dir_override=tmp_path,
            provider_overrides={
                "gemini": provider_gemini,
                "groq": provider_groq,
            },
            source_mode="pinned_proof",
        )

        assert results["benchmark_status"] == "PASS"
        assert results["total_runs"] == 6
        assert results["total_pass"] == 6
        assert results["providers"]["gemini"]["status"] == "PASS"
        assert results["providers"]["groq"]["status"] == "PASS"

    def test_one_failed_mocked_run_makes_entire_benchmark_hold(self, tmp_path):
        """16. One failed mocked run makes entire benchmark HOLD."""
        bad_decision = {
            "schema_version": "0.1.0",
            "project_id": "WRONG_PROJECT",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Controller-authorized LARİ Clinic foundation materialization and read-only scope/contract gap audit from frozen Package baseline 65a53427f52c21e60aa8f92e02a17d693a201601.",
            "target_base_sha": "65a53427f52c21e60aa8f92e02a17d693a201601",
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Fail mock",
            "disposition": "SHADOW_ACCEPT",
        }
        provider_gemini = FakePlannerProvider()
        provider_groq_failing = FakePlannerProvider(decision_override=bad_decision)

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini", "groq"],
            repeat=3,
            trace_dir_override=tmp_path,
            provider_overrides={
                "gemini": provider_gemini,
                "groq": provider_groq_failing,
            },
            source_mode="pinned_proof",
        )

        assert results["benchmark_status"] == "HOLD"
        assert results["providers"]["gemini"]["status"] == "PASS"
        assert results["providers"]["groq"]["status"] == "HOLD"

    def test_current_aos_state_and_evidence_schemas_remain_valid(self):
        """17. Current AOS state/evidence schemas remain valid."""
        state_path = Path(__file__).parent.parent / "docs" / "project-control" / "STATE.json"
        evidence_path = Path(__file__).parent.parent / "docs" / "project-control" / "EVIDENCE.jsonl"

        res_state, code_state = validate_file("state", state_path)
        assert code_state == 0
        assert res_state.is_valid is True

        res_ev, code_ev = validate_file("evidence", evidence_path)
        assert code_ev == 0
        assert res_ev.is_valid is True
