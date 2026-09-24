from pathlib import Path

import pytest
import subprocess

from aos.autonomous_host import (
    ProviderAttemptStatus,
    ProviderFailoverReasoningBackend,
    assert_workspace_execution_lineage,
    build_dag,
    load_bound_run_plan,
)
from aos.planner import PlannerContractError, PlannerTransientError
from aos.provider_observation import ContractFailureSubtype
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


def test_repeated_content_identical_request_gets_distinct_ledger_attempts(tmp_path):
    class UsageChangesPerInvocation:
        execution_provenance = "LOCAL_OFFLINE"

        def __init__(self):
            self.calls = 0

        def generate_plan(self, prompt, schema):
            self.calls += 1
            return {"call": self.calls}, f"response-{self.calls}", {
                "total_tokens": self.calls,
            }

    provider = UsageChangesPerInvocation()
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda _provider_id, _model_id: provider,
        attempt_journal=tmp_path / "attempts.jsonl",
    )
    request = _request(tmp_path)

    first = backend.execute(request)
    second = backend.execute(request)

    assert first.status == "SUCCESS"
    assert second.status == "SUCCESS"
    assert provider.calls == 2
    ledger = backend.resource_ledger
    assert ledger is not None
    summary = ledger.summary()
    assert summary["attempts_started"] == 2
    assert summary["attempts_finished"] == 2
    assert summary["request_count"] == 2
    assert summary["total_tokens"] == 3
    attempt_ids = {
        event.payload["attempt_id"]
        for event in ledger.events(["ATTEMPT_STARTED"])
    }
    assert len(attempt_ids) == 2


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


def test_provider_contract_failure_routes_through_authorized_chain(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        return _ContractFailure()

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())), provider_factory=factory
    )
    result = backend.execute(_request(tmp_path))
    assert result.status == "DEGRADED"
    assert calls == ["nemotron", "gemini", "groq", "ollama"]
    assert result.evidence_payload["failure_class"] == "ALL_ELIGIBLE_REASONING_PROVIDERS_UNAVAILABLE"
    assert result.evidence_payload["provider_attempts"][0]["status"] == ProviderAttemptStatus.NON_RETRYABLE_FAILED.value


def test_contract_subtype_and_safe_detail_survive_attempt_journal(tmp_path):
    class InvalidJsonProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            raise PlannerContractError(
                "invalid output",
                subtype=ContractFailureSubtype.INVALID_JSON,
                safe_detail={"parser_class": "JSONDecodeError", "line": 2, "raw_content": "sk-forbidden"},
            )

    journal = tmp_path / "attempts.jsonl"
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda _provider_id, _model_id: InvalidJsonProvider(),
        attempt_journal=journal,
    )
    result = backend.execute(_request(tmp_path))
    attempt = result.evidence_payload["provider_attempts"][0]
    assert attempt["contract_subtype"] == "INVALID_JSON"
    assert attempt["safe_detail"] == {"parser_class": "JSONDecodeError", "line": 2}
    assert attempt["message"] is None
    assert "sk-forbidden" not in journal.read_text(encoding="utf-8")


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


def test_workspace_execution_lineage_accepts_base_and_descendant_but_rejects_divergence(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("config", "user.email", "aos-test@example.invalid")
    git("config", "user.name", "AOS Test")
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "base.txt")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    assert assert_workspace_execution_lineage(tmp_path, base) == base

    (tmp_path / "child.txt").write_text("child\n", encoding="utf-8")
    git("add", "child.txt")
    git("commit", "-q", "-m", "child")
    child = git("rev-parse", "HEAD")
    assert assert_workspace_execution_lineage(tmp_path, base) == child

    git("checkout", "-q", "--orphan", "divergent")
    git("rm", "-q", "-rf", ".")
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
    git("add", "other.txt")
    git("commit", "-q", "-m", "other")
    with pytest.raises(ValueError, match="not bound to canonical execution lineage"):
        assert_workspace_execution_lineage(tmp_path, base)


def test_dag_requires_live_authority():
    registry = AgentRunRegistry()
    plan = {
        "tasks": [{"node_id": "x", "run_type": "TEST", "authority_id": "NONE"}]
    }
    with pytest.raises(ValueError, match="authority"):
        build_dag("lari", registry, plan)


def test_build_dag_preserves_declared_write_scope_and_expected_artifacts():
    registry = AgentRunRegistry()
    plan = {
        "tasks": [{
            "node_id": "write",
            "run_type": "FILE",
            "authority_id": "AUTH",
            "write_scope": ["src/feature"],
            "expected_artifacts": ["src/feature/result.txt"],
            "payload": {"action": "write_file", "path": "src/feature/result.txt", "content": "ok"},
        }]
    }

    dag = build_dag("lari", registry, plan)

    assert dag.nodes["write"].write_scope == ["src/feature"]
    assert dag.nodes["write"].expected_artifacts == ["src/feature/result.txt"]
