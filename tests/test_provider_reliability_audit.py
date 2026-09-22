"""Deterministic regressions for the provider-reliability and relay provenance audit."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import aos.runtime_server as runtime_server
from aos.autonomous_host import ProviderFailoverReasoningBackend
from aos.controller_relay import ControllerRelayPublisher
from aos.planner import PlannerContractError, PlannerTransientError
from aos.provider_circuit import (
    CREDIT_EXHAUSTED_COOLDOWN_SECONDS,
    CircuitState,
    ProviderCircuitBreakerRegistry,
)
from aos.provider_probe import _classify, local_reasoning_discovery, provider_runtime_matrix
from aos.provider_registry import ProviderRegistry, ProviderRouter
from aos.planning_kernel import _shadow_deliberate
from aos.providers.gemini import GeminiPlannerProvider
from aos.providers.ollama import OllamaPlannerProvider
from aos.providers.openai_compatible import GenericOpenAICompatiblePlannerProvider
from aos.runtime_server import RuntimeEngine
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _policy(*, allow_fallback: bool = True) -> dict:
    return {
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": False,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 0,
        "paid_monthly_budget_usd": 0,
        "allow_provider_fallback": allow_fallback,
        "data_classification": "PUBLIC",
        "risk_routes": {"R0": {"preferred_providers": ["first", "second"]}},
        "providers": {
            provider_id: {
                "provider_id": provider_id,
                "model_id": f"{provider_id}-model",
                "credential_env_var": None,
                "billing_class": "FREE",
                "structured_output": True,
                "cloud_local": "LOCAL",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            }
            for provider_id in ("first", "second")
        },
    }


def _request(tmp_path: Path) -> ExecutionRequest:
    return ExecutionRequest(
        task_id="audit-task",
        project_id="audit",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUDIT",
        payload={"prompt": "probe", "schema": SCHEMA, "risk_class": "R0", "ignore_credentials": True},
    )


def test_half_open_has_single_durable_probe_lease(tmp_path: Path):
    registry = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    due = registry.record_failure("gemini", "TIMEOUT", now=1000.0)

    assert registry.is_provider_available("gemini", now=due) is True
    assert registry.get_circuit("gemini").circuit_state == CircuitState.HALF_OPEN.value
    assert registry.is_provider_available("gemini", now=due + 1.0) is False
    assert registry.is_provider_available("gemini", now=due + 121.0) is True


def test_normal_inference_is_not_misreported_as_probe(tmp_path: Path):
    class Success:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            return {"ok": True}, "response", None

    registry = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda _provider, _model: Success(),
        circuit_registry=registry,
    )

    assert backend.execute(_request(tmp_path)).status == "SUCCESS"
    assert registry.get_circuit("first").probe_count == 0


def test_fallback_policy_false_stops_after_first_invoked_failure(tmp_path: Path):
    calls: list[str] = []

    class Transient:
        def generate_plan(self, prompt, schema):
            raise PlannerTransientError("503 capacity")

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy(allow_fallback=False))),
        provider_factory=lambda provider, _model: calls.append(provider) or Transient(),
    )

    result = backend.execute(_request(tmp_path))
    assert result.status == "DEGRADED"
    assert calls == ["first"]


def test_aggregation_orders_offsets_by_instant_and_resets_stale_failure_count(tmp_path: Path):
    older_success = ProviderCircuitBreakerRegistry(tmp_path / "success.json")
    newer_failure = ProviderCircuitBreakerRegistry(tmp_path / "failure.json")
    older_success.record_success("gemini", observed_at="2026-09-19T11:00:00+02:00")  # 09:00Z
    newer_failure.record_failure(
        "gemini", "RATE_LIMITED", now=1000.0,
        observed_at="2026-09-19T09:30:00+00:00",
    )

    merged = ProviderCircuitBreakerRegistry.aggregate_registries(
        [older_success, newer_failure], now=1001.0
    )
    assert merged.get_circuit("gemini").circuit_state == CircuitState.OPEN.value

    older_success.record_success("gemini", observed_at="2026-09-19T10:00:00+00:00")
    recovered = ProviderCircuitBreakerRegistry.aggregate_registries(
        [older_success, newer_failure], now=1001.0
    ).get_circuit("gemini")
    assert recovered.circuit_state == CircuitState.CLOSED.value
    assert recovered.consecutive_failure_count == 0


def test_shared_probe_id_is_counted_once_across_command_registries(tmp_path: Path):
    first = ProviderCircuitBreakerRegistry(tmp_path / "first.json")
    second = ProviderCircuitBreakerRegistry(tmp_path / "second.json")
    first.record_probe("gemini", probe_id="probe-shared")
    second.record_probe("gemini", probe_id="probe-shared")

    summary = ProviderCircuitBreakerRegistry.aggregate_registries([first, second]).summarize()
    assert summary["provider_probe_count"] == 1


def test_shadow_council_reads_circuit_paths_as_registries(tmp_path: Path, monkeypatch):
    runtime_dir = tmp_path / "commands" / "command-a" / "project-runtime"
    runtime_dir.mkdir(parents=True)
    registry = ProviderCircuitBreakerRegistry(runtime_dir / "provider-circuits.json")
    registry.record_success("gemini", observed_at="2026-09-21T10:00:00+00:00")
    observed: dict = {}

    class Council:
        def __init__(self, **kwargs):
            pass

        def evaluate_decision(self, **kwargs):
            observed.update(kwargs)

    monkeypatch.setattr("aos.planning_kernel.DeliberationCouncilV1", Council)
    monkeypatch.setattr(
        "aos.planning_kernel.assess_council_trigger",
        lambda *_args: SimpleNamespace(council_required=True),
    )
    situation = SimpleNamespace(
        authority_records={},
        project_id="audit",
        identity=lambda: "situation-a",
    )
    _shadow_deliberate(runtime_dir, situation, "PLAN", "prompt", {"ok": True})
    assert observed["is_spare_capacity_available"] is False


def test_not_attempted_observation_does_not_open_circuit(tmp_path: Path):
    registry = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    registry.record_observation(
        "gemini", probe_status="NOT_ATTEMPTED", credential_available=False
    )
    circuit = registry.get_circuit("gemini")
    assert circuit.circuit_state == CircuitState.UNKNOWN.value
    assert circuit.consecutive_failure_count == 0
    assert registry.summarize()["all_reasoning_providers_unavailable"] is True


def test_gemini_and_ollama_fail_closed_on_canonical_schema_violation(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    gemini_response = MagicMock()
    gemini_response.text = '{"ok":"not-a-boolean"}'
    candidate = MagicMock()
    candidate.finish_reason = "STOP"
    gemini_response.candidates = [candidate]
    gemini_client = MagicMock()
    gemini_client.models.generate_content.return_value = gemini_response
    with patch("google.genai.Client", return_value=gemini_client):
        with pytest.raises(PlannerContractError, match="canonical JSON schema validation"):
            GeminiPlannerProvider().generate_plan("probe", SCHEMA)

    ollama_payload = json.dumps({
        "done": True,
        "message": {"content": '{"ok":"not-a-boolean"}'},
    }).encode("utf-8")
    ollama_response = MagicMock()
    ollama_response.read.return_value = ollama_payload
    ollama_response.__enter__.return_value = ollama_response
    with patch("aos.providers.ollama.urllib.request.urlopen", return_value=ollama_response):
        with pytest.raises(PlannerContractError, match="canonical JSON schema validation"):
            OllamaPlannerProvider().generate_plan("probe", SCHEMA)


def test_ollama_probe_requires_the_configured_model(monkeypatch):
    tags = MagicMock()
    tags.read.return_value = json.dumps({"models": [{"name": "other:latest"}]}).encode("utf-8")
    tags.__enter__.return_value = tags
    with patch("aos.provider_probe.urllib.request.urlopen", return_value=tags), patch(
        "aos.provider_probe.OllamaPlannerProvider"
    ) as provider:
        result = local_reasoning_discovery(required_model="llama3.3:70b")
    assert result["state"] == "UNAVAILABLE_CONFIGURED_MODEL_NOT_INSTALLED"
    provider.assert_not_called()


def test_generic_provider_request_profiles_match_contracts(monkeypatch):
    response = MagicMock()
    response.choices = [MagicMock(finish_reason="stop")]
    response.choices[0].message.content = '{"ok":true}'
    response.choices[0].message.refusal = None
    response.usage = None
    client = MagicMock()
    client.chat.completions.create.return_value = response

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account")
    cloudflare = GenericOpenAICompatiblePlannerProvider(
        provider_id="cloudflare",
        model="@cf/model",
        base_url="https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        credential_env_var="CLOUDFLARE_API_TOKEN",
    )
    with patch("openai.OpenAI", return_value=client):
        cloudflare.generate_plan("probe", SCHEMA)
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_schema", "json_schema": SCHEMA}
    assert "store" not in kwargs

    openrouter = GenericOpenAICompatiblePlannerProvider(
        provider_id="openrouter_free", model="model:free", base_url="https://openrouter.ai/api/v1"
    )
    assert openrouter.extra_body["provider"]["require_parameters"] is True


def test_openai_paid_responses_capacity_is_transient(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    response = MagicMock()
    response.status = "incomplete"
    response.incomplete_details = {"reason": "max_output_tokens"}
    client = MagicMock()
    client.responses.create.return_value = response
    provider = GenericOpenAICompatiblePlannerProvider(
        provider_id="openai_paid_safety",
        model="paid-model",
        base_url="https://api.openai.com/v1",
        credential_env_var="OPENAI_API_KEY",
        api_protocol="OPENAI_RESPONSES",
        billing_class="PAID",
    )
    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(PlannerTransientError, match="output capacity"):
            provider.generate_plan("probe", SCHEMA)


def test_huggingface_http_402_is_credit_exhausted_not_contract_failure(monkeypatch):
    import httpx2
    import openai

    monkeypatch.setenv("HF_TOKEN", "fake")
    request = httpx2.Request("POST", "https://router.huggingface.co/v1/chat/completions")
    response = httpx2.Response(402, request=request)
    credit_error = openai.APIStatusError(
        "You have depleted your monthly included credits. Purchase pre-paid credits to continue.",
        response=response,
        body={"error": {"message": "Payment required: depleted monthly included credits"}},
    )
    client = MagicMock()
    client.chat.completions.create.side_effect = credit_error
    provider = GenericOpenAICompatiblePlannerProvider(
        provider_id="huggingface_router",
        model="openai/gpt-oss-120b:fastest",
        base_url="https://router.huggingface.co/v1",
        credential_env_var="HF_TOKEN",
    )

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(PlannerTransientError, match="CREDIT_EXHAUSTED") as caught:
            provider.generate_plan("probe", SCHEMA)
    assert _classify(caught.value) == "CREDIT_EXHAUSTED"
    assert "CONTRACT_FAILURE" not in str(caught.value)


def test_true_http_400_schema_error_remains_contract_failure(monkeypatch):
    import httpx2
    import openai

    monkeypatch.setenv("HF_TOKEN", "fake")
    request = httpx2.Request("POST", "https://router.huggingface.co/v1/chat/completions")
    response = httpx2.Response(400, request=request)
    contract_error = openai.BadRequestError(
        "Unsupported response_format schema",
        response=response,
        body={"error": {"message": "Unsupported response_format schema"}},
    )
    client = MagicMock()
    client.chat.completions.create.side_effect = contract_error
    provider = GenericOpenAICompatiblePlannerProvider(
        provider_id="huggingface_router",
        model="openai/gpt-oss-120b:fastest",
        base_url="https://router.huggingface.co/v1",
        credential_env_var="HF_TOKEN",
    )

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(PlannerContractError, match="invalid request/schema") as caught:
            provider.generate_plan("probe", SCHEMA)
    assert _classify(caught.value) == "CONTRACT_FAILURE"


def test_credit_exhaustion_gets_long_cooldown_then_success_resets(tmp_path: Path):
    registry = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    next_probe = registry.record_failure(
        "huggingface_router", "CREDIT_EXHAUSTED", now=1000.0
    )
    circuit = registry.get_circuit("huggingface_router")
    assert next_probe == 1000.0 + CREDIT_EXHAUSTED_COOLDOWN_SECONDS
    assert circuit.circuit_state == CircuitState.OPEN.value
    assert circuit.last_failure_class == "CREDIT_EXHAUSTED"

    registry.record_success("huggingface_router")
    circuit = registry.get_circuit("huggingface_router")
    assert circuit.circuit_state == CircuitState.CLOSED.value
    assert circuit.consecutive_failure_count == 0
    assert circuit.next_probe_at is None


def test_credit_exhausted_provider_fails_over_to_healthy_alternate(tmp_path: Path):
    calls: list[str] = []

    class CreditExhausted:
        def generate_plan(self, prompt, schema):
            raise PlannerTransientError("CREDIT_EXHAUSTED: payment required")

    class Healthy:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            return {"ok": True}, "response", None

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda provider, _model: (
            calls.append(provider) or (CreditExhausted() if provider == "first" else Healthy())
        ),
        circuit_registry=ProviderCircuitBreakerRegistry(tmp_path / "circuits.json"),
    )

    result = backend.execute(_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["first", "second"]
    assert result.evidence_payload["provider_route"] == "second"
    assert result.evidence_payload["provider_attempts"][0]["error_class"] == "CREDIT_EXHAUSTED"
    assert result.evidence_payload["circuit_summary"]["all_reasoning_providers_unavailable"] is False


def test_paid_probe_requires_all_policy_gates(tmp_path: Path, monkeypatch):
    policy = {
        "allow_paid_fallback": True,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 10,
        "paid_monthly_budget_usd": 100,
        "providers": {
            "openai_paid_safety": {
                "enabled": True,
                "billing_class": "PAID",
                "model_id": "paid-model",
                "credential_env_var": "OPENAI_API_KEY",
            }
        },
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr("aos.provider_probe._hydrate", lambda: None)
    with patch("aos.provider_probe.GenericOpenAICompatiblePlannerProvider") as adapter:
        row = provider_runtime_matrix(path)["openai_paid_safety"]
    assert row["connectivity"] == "NOT_PROBED"
    adapter.assert_not_called()


def _runtime_config(tmp_path: Path, policy: Path) -> dict:
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {"audit": {
            "project_id": "audit",
            "descriptor_path": str(descriptor),
            "workspace": str(workspace),
            "routing_policy_path": str(policy),
            "standing_authority": True,
        }},
        "default_project": "audit",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def test_activation_probe_is_due_bounded_deduplicated_and_paid_gated(tmp_path: Path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "allow_paid_fallback": False,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 0,
        "paid_monthly_budget_usd": 0,
        "providers": {
            "gemini": {"provider_id": "gemini", "enabled": True, "cloud_local": "CLOUD", "billing_class": "FREE", "model_id": "gemini"},
            "openai_paid_safety": {"provider_id": "openai_paid_safety", "enabled": True, "cloud_local": "CLOUD", "billing_class": "PAID", "model_id": "paid"},
        },
    }), encoding="utf-8")
    engine = RuntimeEngine(_runtime_config(tmp_path, policy))
    calls: list[list[str]] = []
    try:
        for command_id in ("command-a", "command-b"):
            engine.store.create_command({
                "command_id": command_id,
                "goal": "audit",
                "project": {**engine.config["projects"]["audit"]},
                "continuous": True,
            })
            engine.store.write_state(command_id, state="WAITING_FOR_REASONING_PROVIDER")

        def fake_probe(_path: Path, provider_ids: list[str]):
            calls.append(provider_ids)
            return {"gemini": {
                "provider_id": "gemini",
                "credential_available": True,
                "local_service_available": None,
                "probe_attempted": True,
                "probe_id": "shared-activation-probe",
                "probe_status": "PASS",
                "failure_class": None,
                "last_observed_at": "2026-09-21T10:00:00+00:00",
                "latency_ms": 5,
            }}

        monkeypatch.setattr(runtime_server, "probe_enabled_providers", fake_probe)
        engine._run_live_probe_cycle()
        assert calls == [["gemini"]]
        status = engine._collect_detailed_status()
        assert status["provider_probe_count"] == 1
        assert status["enabled_reasoning_providers"] == ["gemini"]

        engine._run_live_probe_cycle()
        assert calls == [["gemini"]]
    finally:
        engine.shutdown()


def test_relay_uses_immutable_slot_provenance_after_checkout_advances(tmp_path: Path, monkeypatch):
    source_sha = "6f044bf0cd37fdc09ad5b0b19bb66e5f32170b68"
    tree_sha = "a" * 64
    slot = tmp_path / "candidate" / source_sha
    slot.mkdir(parents=True)
    (slot / "candidate-manifest.json").write_text(json.dumps({
        "provenance": "PROVEN",
        "ci_run_id": 123,
        "candidate_source_sha": source_sha,
        "build_source_sha": source_sha,
        "candidate_tree_sha256": tree_sha,
    }), encoding="utf-8")
    (slot / "build-record.json").write_text(json.dumps({
        "build_source_sha": source_sha,
    }), encoding="utf-8")

    monkeypatch.setattr(
        "aos.controller_relay.get_authoritative_git_head",
        lambda _path: pytest.fail("immutable runtime provenance must not consult mutable checkout HEAD"),
    )
    relay = ControllerRelayPublisher(tmp_path / "relay", {
        "runtime_root": str(tmp_path / "state"),
        "runtime_slot_root": str(slot),
        "authoritative_repo_path": str(tmp_path / "advanced-checkout"),
    })
    snapshot = relay.collect_snapshot(runtime_health_dict={
        "runtime_state": "HEALTHY",
        "runtime_source_sha": source_sha,
        "runtime_slot_root": str(slot),
        "runtime_asset_tree_sha256": tree_sha,
    })
    assert snapshot.provenance_status == "PROVEN"
    assert snapshot.provenance_basis == "IMMUTABLE_RUNTIME_SLOT"
    assert snapshot.provenance_errors == []

    mismatched = relay.collect_snapshot(runtime_health_dict={
        "runtime_state": "HEALTHY",
        "runtime_source_sha": source_sha,
        "runtime_slot_root": str(slot),
        "runtime_asset_tree_sha256": "b" * 64,
    })
    assert mismatched.provenance_status == "FAIL"
    assert "RUNTIME_ASSET_TREE_MISMATCH" in mismatched.provenance_errors
