"""Tests for AOS-2 provider routing, control ingress contract, and cross-provider benchmark."""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aos.planner import (
    FakePlannerProvider,
    OpenAIPlannerProvider,
    PlannerContractError,
    PlannerCredentialError,
    PlannerTransientError,
)
from aos.provider_registry import (
    ProviderExecutionContext,
    ProviderRegistry,
    ProviderRouter,
    RoutingResult,
    load_routing_policy,
)
from aos.providers.gemini import GeminiPlannerProvider, project_gemini_schema
from aos.providers.groq import GroqPlannerProvider, project_groq_schema
from aos.providers.ollama import OllamaPlannerProvider
from aos.source_adapter import ProjectSourceAdapter
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
# 1-3. Source Modes Tests (LIVE_GUARD vs PINNED_PROOF)
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
            mutation_intent="ISOLATED_MUTATION",
        )
        assert disp == "HOLD"
        assert code == 1
        assert len(traces) == 0


# =========================================================================
# 4-10. Provider Router & Eligibility Policy Tests
# =========================================================================

class TestProviderRouterPolicy:
    def test_benchmark_explicit_openai_rejected_by_paid_policy(self, monkeypatch, tmp_path):
        """4. Benchmark explicit OpenAI request is rejected by paid-fallback policy."""
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        adapter = FakeProjectSourceAdapter()
        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["openai"],
            repeat=1,
            trace_dir_override=tmp_path,
            adapter_override=adapter,
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

    def test_eligibility_skip_does_not_set_fallback_used(self, monkeypatch):
        """8. Eligibility skip (e.g. missing credential) does NOT set fallback_used=true."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        policy = _make_policy()
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(risk_class="R0")
        assert res is not None
        assert res.selected_provider_id == "groq"
        assert res.fallback_used is False
        assert res.context.fallback_used is False

    def test_transient_failure_falls_back_when_allowed(self, monkeypatch):
        """9. Post-invocation transient failure falls back when allow_provider_fallback=true."""
        policy = _make_policy({"allow_provider_fallback": True})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(
            risk_class="R0",
            skip_providers=["gemini"],
            ignore_credentials=True,
            post_invocation_failed_provider="gemini",
        )
        assert res is not None
        assert res.selected_provider_id == "groq"
        assert res.fallback_used is True
        assert res.fallback_from == "gemini"
        assert res.context.fallback_used is True

    def test_transient_failure_does_not_fall_back_when_disallowed(self, monkeypatch):
        """10. Post-invocation transient failure does NOT set fallback_used when allow_provider_fallback=false."""
        policy = _make_policy({"allow_provider_fallback": False})
        reg = ProviderRegistry(policy)
        router = ProviderRouter(reg)
        res = router.select(
            risk_class="R0",
            skip_providers=["gemini"],
            ignore_credentials=True,
            post_invocation_failed_provider="gemini",
        )
        assert res is not None
        assert res.selected_provider_id == "groq"
        assert res.fallback_used is False


# =========================================================================
# 11-12. Provider Shopping Abort Tests
# =========================================================================

class TestProviderShoppingAbort:
    def test_semantic_hold_aborts_entire_benchmark(self, tmp_path):
        """11. Semantic/policy HOLD aborts entire benchmark immediately."""
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
        provider_gemini = FakePlannerProvider(decision_override=bad_decision)
        provider_groq = FakePlannerProvider()

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini", "groq"],
            repeat=3,
            trace_dir_override=tmp_path,
            provider_overrides={"gemini": provider_gemini, "groq": provider_groq},
            adapter_override=adapter,
            source_mode="pinned_proof",
        )
        assert results["benchmark_status"] == "HOLD"
        assert provider_gemini.attempts == 1

    def test_groq_attempts_zero_after_earlier_semantic_hold(self, tmp_path):
        """12. Groq attempts remain 0 after Gemini produces a semantic HOLD."""
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
        provider_gemini = FakePlannerProvider(decision_override=bad_decision)
        provider_groq = FakePlannerProvider()

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini", "groq"],
            repeat=3,
            trace_dir_override=tmp_path,
            provider_overrides={"gemini": provider_gemini, "groq": provider_groq},
            adapter_override=adapter,
            source_mode="pinned_proof",
        )
        assert provider_groq.attempts == 0
        assert "groq" not in results["providers"]


# =========================================================================
# 13. Hard Offline Benchmark Regression Test
# =========================================================================

class TestOfflineGuarantee:
    def test_benchmark_fully_offline_network_poisoned(self, monkeypatch, tmp_path):
        """13. Hard offline regression test: network methods poisoned, benchmark must PASS 100% offline."""
        monkeypatch.setattr(ProjectSourceAdapter, "resolve_ref_to_sha", lambda self: pytest.fail("Network call: resolve_ref_to_sha"))
        monkeypatch.setattr(ProjectSourceAdapter, "fetch_file_at_sha", lambda self, p, s: pytest.fail("Network call: fetch_file_at_sha"))
        monkeypatch.setattr(ProjectSourceAdapter, "fetch_canonical_context", lambda self, s, p: pytest.fail("Network call: fetch_canonical_context"))

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
            provider_overrides={"gemini": provider_gemini, "groq": provider_groq},
            adapter_override=adapter,
            source_mode="pinned_proof",
        )

        assert results["benchmark_status"] == "PASS"
        assert results["total_runs"] == 6


# =========================================================================
# 14-16. Trace & Routing Metadata Tests
# =========================================================================

class TestActualGeneratedTraceMetadata:
    def test_actual_gemini_benchmark_trace_routing_identity(self, monkeypatch, tmp_path):
        """14. Test actual trace emitted by run_benchmark for Gemini."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        adapter = FakeProjectSourceAdapter()
        provider_gemini = FakePlannerProvider(model="gemini-3.6-flash")

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["gemini"],
            repeat=1,
            trace_dir_override=tmp_path,
            provider_overrides={"gemini": provider_gemini},
            adapter_override=adapter,
            source_mode="pinned_proof",
        )
        assert results["benchmark_status"] == "PASS"
        trace_id = results["providers"]["gemini"]["runs"][0]["trace_id"]
        trace_path = tmp_path / f"{trace_id}.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))

        assert trace["provider_id"] == "gemini"
        assert trace["model"] == "gemini-3.6-flash"
        assert trace["billing_class"] == "FREE_TIER"
        assert trace["data_classification"] == "PUBLIC"
        assert "selection_reason" in trace
        assert trace["fallback_used"] is False
        assert trace["mutation_performed"] is False

    def test_actual_groq_benchmark_trace_routing_identity(self, monkeypatch, tmp_path):
        """15. Test actual trace emitted by run_benchmark for Groq."""
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        adapter = FakeProjectSourceAdapter()
        provider_groq = FakePlannerProvider(model="openai/gpt-oss-120b")

        results = run_benchmark(
            descriptor_path=str(DESCRIPTOR_PATH),
            expectation_path=str(EXPECTATION_PATH),
            routing_policy_path=str(POLICY_PATH),
            provider_ids=["groq"],
            repeat=1,
            trace_dir_override=tmp_path,
            provider_overrides={"groq": provider_groq},
            adapter_override=adapter,
            source_mode="pinned_proof",
        )
        assert results["benchmark_status"] == "PASS"
        trace_id = results["providers"]["groq"]["runs"][0]["trace_id"]
        trace_path = tmp_path / f"{trace_id}.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))

        assert trace["provider_id"] == "groq"
        assert trace["model"] == "openai/gpt-oss-120b"
        assert trace["billing_class"] == "FREE_TIER"
        assert trace["data_classification"] == "PUBLIC"
        assert "selection_reason" in trace
        assert trace["fallback_used"] is False
        assert trace["mutation_performed"] is False

    def test_generated_trace_contains_no_credential_value(self, monkeypatch, tmp_path):
        """16. Generated trace contains no credential value."""
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
        trace_str = json.dumps(traces[0])
        assert "API_KEY" not in trace_str
        assert "sk-" not in trace_str
        assert "AIza" not in trace_str


# =========================================================================
# 17-18. Gemini Schema Projection Tests
# =========================================================================

class TestGeminiSchemaProjection:
    def test_gemini_schema_projection_strips_unsupported_keywords(self):
        """17. Gemini schema projection strips $schema, pattern, minLength."""
        raw_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.mertsgi.org/aos/v0.1/planner_decision.schema.json",
            "type": "object",
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string", "pattern": "^[a-z0-9_-]+$", "minLength": 1},
            },
        }
        projected = project_gemini_schema(raw_schema)
        assert "$schema" not in projected
        assert "$id" not in projected
        assert "pattern" not in projected["properties"]["project_id"]
        assert "minLength" not in projected["properties"]["project_id"]
        assert projected["properties"]["project_id"]["type"] == "string"

    def test_canonical_validation_catches_pattern_violation_after_gemini_projection(self):
        """18. Malformed output passing reduced Gemini projection is still caught by canonical AOS validation."""
        bad_decision = {
            "schema_version": "0.1.0",
            "project_id": "INVALID PROJECT ID WITH SPACES",
            "source_sha": "4c55eecdbe064c74b34af31a1daf9851689e4fe8",
            "selected_milestone": "LARİ Clinic",
            "selected_next_action": "Action",
            "target_base_sha": None,
            "risk_class": "R0",
            "mutation_intent": "NONE",
            "ambiguity_detected": False,
            "ambiguity_reasons": [],
            "human_gate_required": False,
            "rationale": "Test",
            "disposition": "SHADOW_ACCEPT",
        }
        res = validate_document("planner_decision", bad_decision)
        assert res.is_valid is False
        assert any("pattern" in e.validator for e in res.errors)


# =========================================================================
# 19-20. Groq Schema & Finish Reason Tests
# =========================================================================

class TestGroqSchemaAndCompletion:
    def test_groq_strict_schema_request_contract(self):
        """19. Groq strict schema projection strips meta-schema keywords while preserving strict properties."""
        raw_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.mertsgi.org/aos/v0.1/planner_decision.schema.json",
            "type": "object",
            "required": ["project_id"],
            "additionalProperties": False,
            "properties": {
                "project_id": {"type": "string"},
            },
        }
        projected = project_groq_schema(raw_schema)
        assert "$schema" not in projected
        assert "$id" not in projected
        assert projected["additionalProperties"] is False

    def test_groq_finish_reason_length_fails_closed(self, monkeypatch):
        """20. Groq finish_reason=length fails closed."""
        monkeypatch.setenv("GROQ_API_KEY", "fake-key")
        provider = GroqPlannerProvider()

        mock_choice = MagicMock()
        mock_choice.finish_reason = "length"
        mock_choice.message.content = "incomplete json..."

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_openai_client):
            with pytest.raises(PlannerContractError, match="unacceptable reason: length"):
                provider.generate_plan("test prompt", {})


# =========================================================================
# 21-22. Benchmark Execution Identity Tests
# =========================================================================

class TestBenchmarkExecution:
    def test_six_mocked_gemini_groq_benchmark_runs_preserve_exact_canonical_identity(self, tmp_path):
        """21. Six mocked Gemini/Groq benchmark runs preserve exact canonical identity."""
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
            adapter_override=adapter,
            source_mode="pinned_proof",
        )

        assert results["benchmark_status"] == "PASS"
        assert results["total_runs"] == 6
        assert results["total_pass"] == 6
        assert results["providers"]["gemini"]["status"] == "PASS"
        assert results["providers"]["groq"]["status"] == "PASS"

    def test_one_failed_mocked_run_makes_entire_benchmark_hold(self, tmp_path):
        """22. One failed mocked run makes entire benchmark HOLD."""
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
        adapter = FakeProjectSourceAdapter()
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
            adapter_override=adapter,
            source_mode="pinned_proof",
        )

        assert results["benchmark_status"] == "HOLD"
        assert results["providers"]["gemini"]["status"] == "PASS"


# =========================================================================
# 23-24. Control Request Contract Tests
# =========================================================================

class TestControlRequestContract:
    def test_valid_control_request(self):
        """23. Valid control request passes validation."""
        path = FIXTURES_DIR / "valid" / "control_request.valid.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is True

    def test_force_pass_evidence_rejected(self):
        """24. FORCE_PASS_EVIDENCE is rejected by control_request schema."""
        path = FIXTURES_DIR / "invalid" / "control_request.force_pass.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is False
        assert any("is not one of" in str(e.message) for e in res.errors)

    def test_missing_request_type_rejected(self):
        """24b. Missing request_type is rejected."""
        path = FIXTURES_DIR / "invalid" / "control_request.missing_type.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        res = validate_document("control_request", data)
        assert res.is_valid is False

    def test_no_force_pass_evidence_in_enum(self):
        """24c. Explicitly confirm FORCE_PASS_EVIDENCE is absent from request_type enum."""
        from aos.validate import load_schema
        schema = load_schema("control_request.schema.json")
        allowed_types = schema["properties"]["request_type"]["enum"]
        assert "FORCE_PASS_EVIDENCE" not in allowed_types


# =========================================================================
# 25-29. Schema & Adapter Regressions
# =========================================================================

class TestSchemaAndAdapterRegressions:
    def test_lari_routing_policy_valid(self):
        """25. LARI planner routing policy file is valid against schema."""
        res, code = validate_file("planner_routing_policy", str(POLICY_PATH))
        assert code == 0
        assert res.is_valid is True

    def test_routing_policy_load(self):
        """25b. load_routing_policy returns a valid ProviderRegistry."""
        reg = load_routing_policy(str(POLICY_PATH))
        assert reg.allow_paid_fallback is False
        assert reg.data_classification == "PUBLIC"
        gemini = reg.get_provider("gemini")
        assert gemini is not None
        assert gemini.billing_class == "FREE_TIER"

    def test_planner_schema_accepted_across_all_providers(self):
        """26. Common planner output schema contract."""
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

    def test_ollama_structured_output_mock(self):
        """27. Ollama provider regression."""
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

    def test_ollama_connection_error_raises_transient(self):
        """27b. Ollama connection failure raises PlannerTransientError."""
        import urllib.error
        provider = OllamaPlannerProvider(model="llama3.3:70b")
        with patch("aos.providers.ollama.urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(PlannerTransientError, match="Ollama connection error"):
                provider.generate_plan("test", {})

    def test_openai_provider_mock(self, monkeypatch):
        """28. OpenAI provider regression."""
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        provider = OpenAIPlannerProvider(model="gpt-5.6-sol")

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

        mock_response = MagicMock()
        mock_response.status = "completed"
        mock_response.output_text = decision_json
        mock_response.id = "openai-resp-001"
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.total_tokens = 150

        mock_client = MagicMock()
        mock_client.responses.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client):
            decision, resp_id, usage = provider.generate_plan("test", {})

        assert decision["project_id"] == "lari"

    def test_current_aos_state_and_evidence_schemas_remain_valid(self):
        """29. Current AOS state/evidence schemas remain valid."""
        state_path = Path(__file__).parent.parent / "docs" / "project-control" / "STATE.json"
        evidence_path = Path(__file__).parent.parent / "docs" / "project-control" / "EVIDENCE.jsonl"

        res_state, code_state = validate_file("state", state_path)
        assert code_state == 0
        assert res_state.is_valid is True

        res_ev, code_ev = validate_file("evidence", evidence_path)
        assert code_ev == 0
        assert res_ev.is_valid is True

    def test_gemini_uses_bounded_max_output_tokens_4096(self, monkeypatch):
        """30. Gemini request uses exactly GEMINI_MAX_OUTPUT_TOKENS = 4096."""
        from aos.providers.gemini import GEMINI_MAX_OUTPUT_TOKENS, GeminiPlannerProvider
        assert GEMINI_MAX_OUTPUT_TOKENS == 4096

        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        provider = GeminiPlannerProvider(model="gemini-3.6-flash")

        mock_candidate = MagicMock()
        mock_candidate.finish_reason = "STOP"
        mock_candidate.content.parts = [MagicMock(text='{"project_id": "lari"}')]

        mock_response = MagicMock()
        mock_response.text = '{"project_id": "lari"}'
        mock_response.candidates = [mock_candidate]
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.cached_content_token_count = 0
        mock_response.usage_metadata.candidates_token_count = 50
        mock_response.usage_metadata.total_token_count = 150

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            decision, resp_id, usage = provider.generate_plan("test", {})

        assert decision["project_id"] == "lari"
        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert call_kwargs["config"].max_output_tokens == 4096

    def test_gemini_finish_reason_max_tokens_raises_contract_error_without_fallback(self, monkeypatch, tmp_path):
        """31. FinishReason.MAX_TOKENS raises PlannerContractError and aborts benchmark without fallback or Groq calls."""
        from aos.planner import PlannerContractError
        from aos.providers.gemini import GeminiPlannerProvider

        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        provider = GeminiPlannerProvider(model="gemini-3.6-flash")

        mock_candidate = MagicMock()
        mock_candidate.finish_reason = "FinishReason.MAX_TOKENS"

        mock_response = MagicMock()
        mock_response.text = None
        mock_response.candidates = [mock_candidate]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            with pytest.raises(PlannerContractError) as exc_info:
                provider.generate_plan("test", {})
            assert "FinishReason.MAX_TOKENS" in str(exc_info.value)
