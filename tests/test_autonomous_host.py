from pathlib import Path

import pytest

from aos.autonomous_host import (
    ProviderAttemptStatus,
    ProviderFailoverReasoningBackend,
    build_dag,
    load_bound_run_plan,
)
from aos.planner import PlannerContractError, PlannerTransientError
from aos.provider_registry import ProviderRegistry, ProviderRouter
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest
from extensions.autonomy_fabric.run_registry import AgentRunRegistry


def _policy():
    return {
        "routing_mode": "PREFER_FREE",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {"R0": {"preferred_providers": ["nemotron", "gemini", "groq", "ollama"]}},
        "providers": {
            name: {
                "provider_id": name,
                "model_id": f"{name}-model",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "LOCAL" if name == "ollama" else "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
            }
            for name in ("nemotron", "gemini", "groq", "ollama")
        },
    }


class _Transient:
    execution_provenance = "LOCAL_OFFLINE"

    def generate_plan(self, prompt, schema):
        raise PlannerTransientError("429 quota exhausted")


class _Success:
    execution_provenance = "LOCAL_OFFLINE"

    def __init__(self, provider_id):
        self.provider_id = provider_id

    def generate_plan(self, prompt, schema):
        return {"provider": self.provider_id}, "response-1", {"total_tokens": 1}


class _ContractFailure:
    execution_provenance = "LOCAL_OFFLINE"

    def generate_plan(self, prompt, schema):
        raise PlannerContractError("schema corruption")


def _request(tmp_path):
    return ExecutionRequest(
        task_id="reason-1",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "plan", "schema": {"type": "object"}, "risk_class": "R0", "ignore_credentials": True},
    )


def test_post_invocation_transient_failure_advances_provider(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        return _Transient() if provider_id == "nemotron" else _Success(provider_id)

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
    )
    result = backend.execute(_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls[:2] == ["nemotron", "gemini"]
    attempts = result.evidence_payload["provider_attempts"]
    assert attempts[0]["status"] == ProviderAttemptStatus.QUOTA_EXHAUSTED.value
    assert attempts[1]["status"] == ProviderAttemptStatus.SUCCESS.value
    assert result.evidence_payload["fallback_used"] is True


def test_exhausted_transient_providers_preserve_waiting_failure_class(tmp_path):
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda provider_id, model_id: _Transient(),
    )
    result = backend.execute(_request(tmp_path))

    assert result.status == "DEGRADED"
    assert result.evidence_payload["failure_class"] == "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"
    assert [attempt["provider_id"] for attempt in result.evidence_payload["provider_attempts"]] == [
        "nemotron",
        "gemini",
        "groq",
        "ollama",
    ]


def test_contract_failure_is_not_routed_around(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        return _ContractFailure()

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())), provider_factory=factory
    )
    result = backend.execute(_request(tmp_path))
    assert result.status == "FAILED"
    assert calls == ["nemotron"]
    assert result.evidence_payload["provider_attempts"][0]["status"] == ProviderAttemptStatus.NON_RETRYABLE_FAILED.value


def test_stale_run_plan_is_rejected(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        '{"schema_version":"1.0.0","project_id":"lari","bound_source_sha":"' + "a" * 40 + '","tasks":[{"node_id":"x","run_type":"TEST","authority_id":"AUTH"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stale"):
        load_bound_run_plan(plan, "lari", "b" * 40)


def test_stale_execution_base_run_plan_is_rejected(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        (
            '{"schema_version":"1.0.0","project_id":"lari",'
            '"bound_source_sha":"' + "a" * 40 + '",'
            '"bound_execution_base_sha":"' + "c" * 40 + '",'
            '"tasks":[{"node_id":"x","run_type":"TEST","authority_id":"AUTH"}]}'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bound_execution_base_sha"):
        load_bound_run_plan(plan, "lari", "a" * 40, "b" * 40)


def test_exact_execution_base_run_plan_is_accepted(tmp_path):
    plan = tmp_path / "plan.json"
    expected_base = "b" * 40
    plan.write_text(
        (
            '{"schema_version":"1.0.0","project_id":"lari",'
            '"bound_source_sha":"' + "a" * 40 + '",'
            '"bound_execution_base_sha":"' + expected_base + '",'
            '"tasks":[{"node_id":"x","run_type":"TEST","authority_id":"AUTH"}]}'
        ),
        encoding="utf-8",
    )
    loaded = load_bound_run_plan(plan, "lari", "a" * 40, expected_base)
    assert loaded["bound_execution_base_sha"] == expected_base


def test_execution_base_binding_fails_closed_when_canonical_base_missing(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(
        (
            '{"schema_version":"1.0.0","project_id":"lari",'
            '"bound_source_sha":"' + "a" * 40 + '",'
            '"bound_execution_base_sha":"' + "b" * 40 + '",'
            '"tasks":[{"node_id":"x","run_type":"TEST","authority_id":"AUTH"}]}'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="canonical source exposes no execution base"):
        load_bound_run_plan(plan, "lari", "a" * 40)


def test_dag_requires_live_authority():
    registry = AgentRunRegistry()
    plan = {
        "tasks": [{"node_id": "x", "run_type": "TEST", "authority_id": "NONE"}]
    }
    with pytest.raises(ValueError, match="authority"):
        build_dag("lari", registry, plan)
