"""Deterministic chaos and failover test matrix for AOS Provider Fabric V2."""

from pathlib import Path
import json
import os
import time
import pytest

from aos.autonomous_host import (
    ProviderAttemptStatus,
    ProviderFailoverReasoningBackend,
)
from aos.planner import PlannerContractError, PlannerTransientError
from aos.provider_circuit import (
    CircuitState,
    ProviderCircuitBreakerRegistry,
)
from aos.provider_registry import ProviderRegistry, ProviderRouter
from aos.providers import GenericOpenAICompatiblePlannerProvider
from aos.runtime_store import RuntimeStore
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest


def _v2_policy(
    allow_paid_fallback: bool = False,
    paid_fallback_enabled: bool = False,
    paid_daily_budget_usd: float = 0.0,
    paid_monthly_budget_usd: float = 0.0,
):
    return {
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": allow_paid_fallback,
        "paid_fallback_enabled": paid_fallback_enabled,
        "paid_daily_budget_usd": paid_daily_budget_usd,
        "paid_monthly_budget_usd": paid_monthly_budget_usd,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {
            "R0": {
                "preferred_providers": [
                    "nemotron",
                    "gemini",
                    "groq",
                    "cloudflare",
                    "openrouter_free",
                    "cerebras",
                    "huggingface_router",
                    "ollama",
                    "openai_paid_safety",
                ]
            }
        },
        "providers": {
            "nemotron": {
                "provider_id": "nemotron",
                "model_id": "nvidia/nemotron-3-ultra-550b-a55b",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "gemini": {
                "provider_id": "gemini",
                "model_id": "gemini-3.6-flash",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "groq": {
                "provider_id": "groq",
                "model_id": "openai/gpt-oss-120b",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "cloudflare": {
                "provider_id": "cloudflare",
                "model_id": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "credential_env_var": None,
                "billing_class": "FREE_DAILY_QUOTA",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "openrouter_free": {
                "provider_id": "openrouter_free",
                "model_id": "meta-llama/llama-3.3-70b-instruct:free",
                "credential_env_var": None,
                "billing_class": "FREE",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "cerebras": {
                "provider_id": "cerebras",
                "model_id": "llama-3.3-70b",
                "credential_env_var": None,
                "billing_class": "FREE_TRIAL",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
            "huggingface_router": {
                "provider_id": "huggingface_router",
                "model_id": "meta-llama/Llama-3.3-70B-Instruct:fastest",
                "credential_env_var": None,
                "billing_class": "FREE",
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
                "allowed_data_classifications": ["PUBLIC"],
            },
            "openai_paid_safety": {
                "provider_id": "openai_paid_safety",
                "model_id": "gpt-5.6-luna",
                "credential_env_var": None,
                "billing_class": "PAID",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            },
        },
    }


class _MockProvider:
    execution_provenance = "LOCAL_OFFLINE"

    def __init__(self, provider_id: str, should_fail: bool = False, error_msg: str = "429 rate limited"):
        self.provider_id = provider_id
        self.should_fail = should_fail
        self.error_msg = error_msg

    def generate_plan(self, prompt, schema):
        if self.should_fail:
            raise PlannerTransientError(self.error_msg)
        return {"provider": self.provider_id, "status": "ok"}, "resp-id", {"tokens": 50}


def _make_req(tmp_path: Path):
    return ExecutionRequest(
        task_id="chaos-task-1",
        project_id="lari",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "test", "schema": {"type": "object"}, "risk_class": "R0", "ignore_credentials": True},
    )


def test_full_free_first_failover_ladder(tmp_path):
    """
    Test deterministic progression:
    nemotron unavail -> gemini
    gemini unavail -> groq
    groq unavail -> cloudflare
    cloudflare unavail -> openrouter_free
    openrouter_free unavail -> cerebras
    cerebras unavail -> huggingface_router
    huggingface_router unavail -> ollama
    """
    ladder = [
        "nemotron",
        "gemini",
        "groq",
        "cloudflare",
        "openrouter_free",
        "cerebras",
        "huggingface_router",
        "ollama",
    ]

    for idx, expected_winner in enumerate(ladder):
        calls = []
        failing_providers = set(ladder[:idx])

        def factory(provider_id, model_id):
            calls.append(provider_id)
            if provider_id in failing_providers:
                return _MockProvider(provider_id, should_fail=True, error_msg=f"{provider_id} capacity exhausted")
            return _MockProvider(provider_id, should_fail=False)

        backend = ProviderFailoverReasoningBackend(
            ProviderRouter(ProviderRegistry(_v2_policy(allow_paid_fallback=False))),
            provider_factory=factory,
            attempt_journal=tmp_path / f"attempts_{idx}.jsonl",
            circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / f"circuits_{idx}.json"),
        )

        res = backend.execute(_make_req(tmp_path))
        assert res.status == "SUCCESS", f"Failed on step {expected_winner}: {res.status}"
        assert res.evidence_payload["provider_route"] == expected_winner
        assert calls[-1] == expected_winner


def test_all_free_unavailable_and_paid_disabled_fails_to_waiting_without_calling_paid(tmp_path):
    """
    All FREE/TRIAL/LOCAL unavailable AND paid disabled
    -> WAITING_FOR_REASONING_PROVIDER (or non-SUCCESS status)
    NOT paid execution. OpenAI call count MUST equal zero.
    """
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id == "openai_paid_safety":
            pytest.fail("Paid provider was invoked when allow_paid_fallback=False!")
        return _MockProvider(provider_id, should_fail=True, error_msg=f"{provider_id} down")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_v2_policy(allow_paid_fallback=False))),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts_all_down.jsonl",
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits_all_down.json"),
    )

    res = backend.execute(_make_req(tmp_path))
    assert res.status != "SUCCESS"
    assert "openai_paid_safety" not in calls


def test_paid_fallback_fails_closed_when_budget_or_gate_zero(tmp_path):
    """
    Deterministically test that allow_paid_fallback=True fails closed if
    paid_fallback_enabled is False or daily/monthly budgets are zero.
    """
    # Case 1: allow_paid_fallback=True, but paid_fallback_enabled=False
    policy1 = _v2_policy(
        allow_paid_fallback=True,
        paid_fallback_enabled=False,
        paid_daily_budget_usd=10.0,
        paid_monthly_budget_usd=100.0,
    )
    router1 = ProviderRouter(ProviderRegistry(policy1))
    # All free down, should not select paid
    assert router1.select(
        risk_class="R0",
        skip_providers=[
            "nemotron", "gemini", "groq", "cloudflare",
            "openrouter_free", "cerebras", "huggingface_router", "ollama",
        ],
    ) is None

    # Case 2: allow_paid_fallback=True, paid_fallback_enabled=True, but daily_budget=0
    policy2 = _v2_policy(
        allow_paid_fallback=True,
        paid_fallback_enabled=True,
        paid_daily_budget_usd=0.0,
        paid_monthly_budget_usd=100.0,
    )
    router2 = ProviderRouter(ProviderRegistry(policy2))
    assert router2.select(
        risk_class="R0",
        skip_providers=[
            "nemotron", "gemini", "groq", "cloudflare",
            "openrouter_free", "cerebras", "huggingface_router", "ollama",
        ],
    ) is None

    # Case 3: allow_paid_fallback=True, paid_fallback_enabled=True, but monthly_budget=0
    policy3 = _v2_policy(
        allow_paid_fallback=True,
        paid_fallback_enabled=True,
        paid_daily_budget_usd=10.0,
        paid_monthly_budget_usd=0.0,
    )
    router3 = ProviderRouter(ProviderRegistry(policy3))
    assert router3.select(
        risk_class="R0",
        skip_providers=[
            "nemotron", "gemini", "groq", "cloudflare",
            "openrouter_free", "cerebras", "huggingface_router", "ollama",
        ],
    ) is None


def test_paid_enabled_allows_paid_safety_net_when_free_exhausted(tmp_path):
    """
    When allow_paid_fallback=True, paid_fallback_enabled=True, and budgets > 0,
    and all free options exhausted, openai_paid_safety is invoked.
    """
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id == "openai_paid_safety":
            return _MockProvider(provider_id, should_fail=False)
        return _MockProvider(provider_id, should_fail=True, error_msg="Free tier exhausted")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(
            ProviderRegistry(
                _v2_policy(
                    allow_paid_fallback=True,
                    paid_fallback_enabled=True,
                    paid_daily_budget_usd=10.0,
                    paid_monthly_budget_usd=100.0,
                )
            )
        ),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts_paid_ok.jsonl",
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits_paid_ok.json"),
    )

    res = backend.execute(_make_req(tmp_path))
    assert res.status == "SUCCESS"
    assert res.evidence_payload["provider_route"] == "openai_paid_safety"
    assert "openai_paid_safety" in calls


def test_generic_openai_compatible_provider_instantiation():
    """Verify GenericOpenAICompatiblePlannerProvider initialization and properties."""
    p = GenericOpenAICompatiblePlannerProvider(
        provider_id="cloudflare",
        model="@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        base_url="https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        credential_env_var="CLOUDFLARE_API_TOKEN",
        api_protocol="OPENAI_CHAT_COMPLETIONS",
        max_output_tokens=2500,
        billing_class="FREE_DAILY_QUOTA",
    )
    assert p.provider_id == "cloudflare"
    assert p.billing_class == "FREE_DAILY_QUOTA"
    assert p.api_protocol == "OPENAI_CHAT_COMPLETIONS"


def test_sanitized_providers_metadata_in_control_panel():
    from aos.control_panel import _get_sanitized_providers

    sanitized = _get_sanitized_providers(None, {})
    assert len(sanitized) >= 8
    c_item = next(p for p in sanitized if p["provider_id"] == "cerebras")
    assert c_item["display_name"] == "Cerebras Free Trial"
    assert c_item["credential_env_var"] == "CEREBRAS_API_KEY"
    assert c_item["billing_class"] == "FREE_TRIAL"
    assert c_item["cloud_local"] == "CLOUD"
    assert c_item["enabled"] is True
    assert c_item["credential_configurable"] is True
    assert c_item["provider_console_url"] == "https://cloud.cerebras.ai/"
    assert "api_key" not in c_item
    assert "token" not in c_item

    o_item = next(p for p in sanitized if p["provider_id"] == "ollama")
    assert o_item["credential_configurable"] is False
    assert o_item["display_name"] == "Ollama (Local)"


def test_council_capacity_guard_and_share_enforcement(tmp_path):
    from aos.providers.council import DeliberationCouncilV1

    ledger_dir = tmp_path / "deliberation"
    ledger_dir.mkdir(parents=True, exist_ok=True)

    council = DeliberationCouncilV1(
        mode="SHADOW_ONLY",
        ledger_dir=ledger_dir,
    )

    primary = {
        "title": "Primary Architecture",
        "authority_id": "DECISION-020",
        "rationale": "Canonical RPC endpoint",
        "tasks": [{"node_id": "task-1", "action": "rpc_call"}],
    }

    # spare_capacity must be False when lane is waiting or no spare capacity
    res = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide architecture pattern",
        primary_proposal=primary,
        is_spare_capacity_available=False,
    )
    assert res.decision_record["council_status"] == "SKIPPED_DUE_TO_SPARE_CAPACITY_CONSTRAINTS"
    assert council.skipped_capacity_count == 1

    # Now test budget share limit (10%)
    # Let primary call count be 10 and council call count be 2 -> anticipated share > 10%
    council.primary_provider_call_count = 10
    council.council_provider_call_count = 2
    res2 = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide architecture pattern 2",
        primary_proposal={"title": "Primary 2"},
        is_spare_capacity_available=True,
    )
    assert res2.decision_record["council_status"] == "SKIPPED_DUE_TO_BUDGET_SHARE_LIMIT"

    # With primary call count 100 and council call count 1 -> anticipated share = 1 / 101 <= 10%
    council.primary_provider_call_count = 100
    council.council_provider_call_count = 1
    res3 = council.evaluate_decision(
        decision_type="ARCHITECTURE_DECISION",
        prompt="Decide architecture pattern 3",
        primary_proposal={"title": "Primary 3"},
        is_spare_capacity_available=True,
    )
    assert res3.decision_record["council_status"] != "SKIPPED_DUE_TO_BUDGET_SHARE_LIMIT"

