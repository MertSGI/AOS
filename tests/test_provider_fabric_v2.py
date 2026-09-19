"""Deterministic chaos and failover test matrix for AOS Provider Fabric V2."""

from pathlib import Path
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


def _v2_policy(allow_paid_fallback: bool = False):
    return {
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": allow_paid_fallback,
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


def test_paid_enabled_allows_paid_safety_net_when_free_exhausted(tmp_path):
    """
    When allow_paid_fallback=True and all free options exhausted,
    openai_paid_safety is invoked.
    """
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id == "openai_paid_safety":
            return _MockProvider(provider_id, should_fail=False)
        return _MockProvider(provider_id, should_fail=True, error_msg="Free tier exhausted")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_v2_policy(allow_paid_fallback=True))),
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
