import json

from aos.autonomous_host import ProviderFailoverReasoningBackend
from aos.provider_circuit import CircuitState, ProviderCircuitBreakerRegistry
from aos.provider_observation import ObservationSource, RateLimitObservation
from aos.provider_registry import ProviderRegistry, ProviderRouter
from aos.quota_governor import QuotaGovernor, QuotaState
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest


def _observation(
    *,
    retry_at=1120.0,
    source=ObservationSource.PROVIDER_METADATA.value,
    classification="RATE_LIMITED",
    request_remaining=0,
):
    return RateLimitObservation(
        provider_id="nemotron",
        model_id="nemotron-model",
        task_class="structured_planning",
        observed_at="2026-09-23T00:00:00+00:00",
        http_status=429,
        classification=classification,
        retry_at_epoch=retry_at,
        request_limit=30,
        request_remaining=request_remaining,
        quota_scope="MODEL",
        evidence_source=source,
        field_sources={
            "retry_at_epoch": source,
            "request_limit": source,
            "request_remaining": source,
        },
    )


def test_exact_quota_deadline_blocks_then_expires_without_health_change(tmp_path):
    now = [1000.0]
    governor = QuotaGovernor(tmp_path / "quota.json", clock=lambda: now[0])
    circuit = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    circuit.record_success(
        "nemotron", model_id="nemotron-model", task_class="structured_planning"
    )

    decision = governor.record(_observation())

    assert decision.state == QuotaState.EXHAUSTED.value
    assert decision.eligible is False
    assert decision.retry_at_epoch == 1120.0
    health = circuit.get_health_record(
        "nemotron", model_id="nemotron-model", task_class="structured_planning"
    )
    assert health["circuit_state"] == CircuitState.CLOSED.value
    assert health["family_streaks"] == {}

    now[0] = 1120.0
    reopened = governor.decision(
        "nemotron", "nemotron-model", "structured_planning"
    )
    assert reopened.eligible is True
    assert reopened.reason == "WINDOW_EXPIRED"


def test_provider_metadata_replaces_adaptive_estimate_per_field():
    now = [1000.0]
    governor = QuotaGovernor(clock=lambda: now[0])
    estimate = _observation(
        retry_at=1060.0,
        source=ObservationSource.ADAPTIVE_ESTIMATE.value,
    )
    provider = _observation(retry_at=1120.0)

    assert governor.record(estimate).retry_at_epoch == 1060.0
    assert governor.record(provider).retry_at_epoch == 1120.0
    record = next(iter(governor.snapshot()["records"].values()))
    assert record["field_sources"]["retry_at_epoch"] == "PROVIDER_METADATA"


def test_restart_preserves_exact_deadline_and_corruption_fails_closed(tmp_path):
    path = tmp_path / "resource-os" / "quota-governor.json"
    first = QuotaGovernor(path, clock=lambda: 1000.0)
    first.record(_observation(retry_at=1400.0))

    restarted = QuotaGovernor(path, clock=lambda: 1050.0)
    decision = restarted.decision(
        "nemotron", "nemotron-model", "structured_planning"
    )
    assert decision.eligible is False
    assert decision.retry_at_epoch == 1400.0

    path.write_text("{truncated", encoding="utf-8")
    corrupt = QuotaGovernor(path, clock=lambda: 1050.0)
    decision = corrupt.decision("nemotron", "nemotron-model", "structured_planning")
    assert decision.eligible is False
    assert decision.reason == "CORRUPT_QUOTA_STATE_FAIL_CLOSED"


def _policy():
    return {
        "routing_mode": "PREFER_FREE",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {"R0": {"preferred_providers": ["nemotron", "gemini"]}},
        "providers": {
            name: {
                "provider_id": name,
                "model_id": f"{name}-model",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            }
            for name in ("nemotron", "gemini")
        },
    }


def test_backend_skips_quota_blocked_provider_without_calling_it(tmp_path):
    calls = []
    governor = QuotaGovernor(clock=lambda: 1000.0)
    governor.record(_observation())

    class Success:
        execution_provenance = "LOCAL_OFFLINE"

        def __init__(self, provider_id):
            self.provider_id = provider_id

        def generate_plan(self, prompt, schema):
            return {"provider": self.provider_id}, "response", {"total_tokens": 1}

    def factory(provider_id, _model_id):
        calls.append(provider_id)
        return Success(provider_id)

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        quota_governor=governor,
    )
    result = backend.execute(ExecutionRequest(
        task_id="quota-route",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "plan",
            "schema": {"type": "object"},
            "risk_class": "R0",
            "ignore_credentials": True,
            "task_class": "structured_planning",
        },
    ))

    assert result.status == "SUCCESS"
    assert calls == ["gemini"]
    first_attempt = result.evidence_payload["provider_attempts"][0]
    assert first_attempt["provider_id"] == "nemotron"
    assert first_attempt["status"] == "QUOTA_EXHAUSTED"
    assert first_attempt["quota_decision"]["retry_at_epoch"] == 1120.0
    assert "raw" not in json.dumps(first_attempt).lower()
