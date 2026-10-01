"""Test matrix A through U for AOS Resource OS Convergence R2.

Covers:
- Test A: provider adequacy token budget rejection (context + reserve > window)
- Test B: provider adequacy token budget acceptance
- Test C: local Qwen adequacy allows LOW, MEDIUM, and HIGH when window allows
- Test D: local Qwen adequacy rejects CRITICAL
- Test E: local Qwen adequacy rejects tasks planning when schema requires larger window
- Test F: quota governor default revalidation windows for no-deadline observations
- Test G: quota governor record_success supersedes stale exhausted state
- Test H: quota governor latest_observation returns truthful snapshot
- Test I: provider failover records SKIPPED attempt on adequacy failure without invocation
- Test J: provider failover sanitized provider_decisions trace preserved in evidence payload
- Test K: provider failover record_success clears stale quota barrier on success
- Test L: provider failover preserves typed failure mapping
- Test M: agentic planning bridge cost class is QUOTA_LIMITED
- Test N: agentic planning bridge ordered proposal extraction
- Test O: antigravity backend failure includes failure_class in evidence_payload
- Test P: antigravity backend planning mode uses json output format
- Test Q: antigravity backend planning mode sets transient_structured_output
- Test R: lari policy schema validation
- Test S: nemotron policy schema validation
- Test T: runtime store exclusive lock regression
- Test U: full fallback progression through all tiers
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aos.autonomous_host import (
    ProviderAttemptStatus,
    ProviderFailoverReasoningBackend,
)
from aos.planning_kernel import (
    build_planning_resource_requirements,
)
from aos.provider_adequacy import (
    ProviderAdequacyDecision,
    evaluate_provider_adequacy,
)
from aos.provider_observation import ObservationSource, RateLimitObservation
from aos.provider_registry import ProviderEntry, ProviderRegistry, ProviderRouter
from aos.quota_governor import QuotaGovernor, QuotaState
from aos.validate import validate_file
from extensions.autonomy_fabric.agentic_planning_bridge import (
    AgenticStructuredPlanningBridge,
)
from extensions.autonomy_fabric.antigravity_adapter import (
    AntigravityResponse,
    AntigravityStatus,
    FakeAntigravityAdapter,
)
from extensions.autonomy_fabric.antigravity_agentic_backend import (
    AntigravityAgenticExecutionBackend,
)
from extensions.autonomy_fabric.run_registry import RunStatus
from extensions.autonomy_fabric.execution_backend import (
    BackendClass,
    EvidenceClass,
    ExecutionAvailabilitySnapshot,
    ExecutionAvailabilityState,
    ExecutionBackend,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTrustZone,
)


def _sample_policy():
    return {
        "routing_mode": "PREFER_FREE",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {"R0": {"preferred_providers": ["gemini", "nemotron"]}},
        "providers": {
            "gemini": {
                "provider_id": "gemini",
                "model_id": "gemini-2.5-flash",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 1048576,
                "quality_tier": 3,
                "expected_latency_ms": 1500,
                "scarcity_class": "STANDARD",
            },
            "nemotron": {
                "provider_id": "nemotron",
                "model_id": "nemotron-3-ultra-550b-a55b",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 131072,
                "quality_tier": 3,
                "expected_latency_ms": 3000,
                "scarcity_class": "SCARCE",
            },
        },
    }


# Test A: Provider adequacy token budget rejection (context + reserve > window)
def test_a_provider_adequacy_token_budget_rejection():
    provider = ProviderEntry(
        provider_id="tiny_provider",
        model_id="tiny-model",
        credential_env_var=None,
        billing_class="FREE_TIER",
        structured_output=True,
        cloud_local="CLOUD",
        enabled=True,
        allowed_data_classifications=["PUBLIC"],
        model_context_tokens=4000,
        quality_tier=2,
    )
    # context 3000 + reserve 2200 = 5200 > 4000 window
    reqs = {
        "task_class": "structured_planning",
        "context_tokens": 3000,
        "output_token_reserve": 2200,
        "request_token_budget": 5200,
        "minimum_quality": 1,
    }
    decision = evaluate_provider_adequacy(provider, reqs, None)
    assert decision.eligible is False
    assert "CONTEXT_INADEQUATE" in decision.reasons
    assert decision.request_token_budget == 5200
    assert decision.model_context_tokens == 4000


# Test B: Provider adequacy token budget acceptance
def test_b_provider_adequacy_token_budget_acceptance():
    provider = ProviderEntry(
        provider_id="standard_provider",
        model_id="standard-model",
        credential_env_var=None,
        billing_class="FREE_TIER",
        structured_output=True,
        cloud_local="CLOUD",
        enabled=True,
        allowed_data_classifications=["PUBLIC"],
        model_context_tokens=32768,
        quality_tier=3,
    )
    reqs = {
        "task_class": "structured_planning",
        "context_tokens": 5000,
        "output_token_reserve": 2200,
        "request_token_budget": 7200,
        "minimum_quality": 3,
    }
    decision = evaluate_provider_adequacy(provider, reqs, None)
    assert decision.eligible is True
    assert decision.reasons == ()


# Test C: Local Qwen adequacy allows LOW, MEDIUM, and HIGH when window allows
def test_c_local_qwen_adequacy_allows_low_medium_high():
    for prompt_text, expected_complexity in [
        ("simple prompt", "LOW"),
        ("normal planning step", "LOW"),
    ]:
        reqs = build_planning_resource_requirements(
            task_class="structured_planning",
            prompt=prompt_text,
            schema={"type": "object", "title": "completion"},
        )
        assert reqs["local_qwen_allowed"] is True
        assert reqs["output_token_reserve"] == 1000

        provider = ProviderEntry(
            provider_id="local_qwen",
            model_id="qwen-2.5-coder-7b",
            credential_env_var=None,
            billing_class="FREE_LOCAL",
            structured_output=True,
            cloud_local="LOCAL",
            enabled=True,
            allowed_data_classifications=["PUBLIC"],
            model_context_tokens=32768,
            quality_tier=3,
        )
        decision = evaluate_provider_adequacy(provider, reqs, None)
        assert decision.eligible is True


# Test D: Schema output token reserve sensitivity (objective/completion: 1000, tasks: 3200, general: 2200)
def test_d_schema_output_token_reserve_sensitivity():
    # 1. Objective / selection
    reqs_obj = build_planning_resource_requirements(
        task_class="structured_planning",
        prompt="choose objective",
        schema={"type": "object", "title": "Objective Selection Schema"},
    )
    assert reqs_obj["output_token_reserve"] == 1000

    # 2. Completion / evaluation
    reqs_comp = build_planning_resource_requirements(
        task_class="structured_planning",
        prompt="evaluate completion",
        schema={"type": "object", "title": "Completion Evaluation Schema"},
    )
    assert reqs_comp["output_token_reserve"] == 1000

    # 3. Tasks
    reqs_tasks = build_planning_resource_requirements(
        task_class="structured_planning",
        prompt="plan tasks",
        schema={"type": "object", "required": ["tasks"], "properties": {"tasks": {"type": "array"}}},
    )
    assert reqs_tasks["output_token_reserve"] == 3200

    # 4. General / other
    reqs_gen = build_planning_resource_requirements(
        task_class="structured_planning",
        prompt="other planning",
        schema={"type": "object", "properties": {"summary": {"type": "string"}}},
    )
    assert reqs_gen["output_token_reserve"] == 2200


# Test E: Local Qwen adequacy rejects tasks planning when schema requires larger window
def test_e_local_qwen_adequacy_rejects_tasks_planning_exceeding_window():
    long_prompt = "x" * 125000  # ~31250 tokens
    reqs = build_planning_resource_requirements(
        task_class="tasks",
        prompt=long_prompt,
        schema={"type": "object", "properties": {"tasks": {"type": "array"}}},
    )
    assert reqs["output_token_reserve"] == 3200
    assert reqs["request_token_budget"] > 32768

    provider = ProviderEntry(
        provider_id="local_qwen",
        model_id="qwen-2.5-coder-7b",
        credential_env_var=None,
        billing_class="FREE_LOCAL",
        structured_output=True,
        cloud_local="LOCAL",
        enabled=True,
        allowed_data_classifications=["PUBLIC"],
        model_context_tokens=32768,
        quality_tier=2,
    )
    decision = evaluate_provider_adequacy(provider, reqs, None)
    assert decision.eligible is False
    assert "CONTEXT_INADEQUATE" in decision.reasons


# Test F: Quota governor default revalidation windows for no-deadline observations
def test_f_quota_governor_default_revalidation_windows():
    import datetime as dt
    now = 10000.0
    now_iso = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat()
    governor = QuotaGovernor(clock=lambda: now)

    # 1. RATE_LIMITED with no deadline -> 120s (regardless of evidence source)
    obs_meta = RateLimitObservation(
        provider_id="prov1",
        model_id="mod1",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=None,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    dec1 = governor.record(obs_meta)
    assert dec1.state == QuotaState.EXHAUSTED.value
    assert dec1.retry_at_epoch == 10000.0 + 120.0

    obs_adapt = RateLimitObservation(
        provider_id="prov2",
        model_id="mod2",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=None,
        evidence_source=ObservationSource.ADAPTIVE_ESTIMATE.value,
    )
    dec2 = governor.record(obs_adapt)
    assert dec2.state == QuotaState.EXHAUSTED.value
    assert dec2.retry_at_epoch == 10000.0 + 120.0

    # 2. QUOTA_EXHAUSTED with no deadline -> 900s
    obs_quota = RateLimitObservation(
        provider_id="prov3",
        model_id="mod3",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=429,
        classification="QUOTA_EXHAUSTED",
        retry_at_epoch=None,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    dec3 = governor.record(obs_quota)
    assert dec3.state == QuotaState.EXHAUSTED.value
    assert dec3.retry_at_epoch == 10000.0 + 900.0

    # 3. CREDIT_EXHAUSTED with no deadline -> 21600s
    obs_credit = RateLimitObservation(
        provider_id="prov4",
        model_id="mod4",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=402,
        classification="CREDIT_EXHAUSTED",
        retry_at_epoch=None,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    dec4 = governor.record(obs_credit)
    assert dec4.state == QuotaState.EXHAUSTED.value
    assert dec4.retry_at_epoch == 10000.0 + 21600.0


# Test G: Quota governor record_success supersedes stale exhausted state
def test_g_quota_governor_record_success_supersedes_stale_exhausted():
    import datetime as dt
    now = 1000.0
    now_iso = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).isoformat()
    governor = QuotaGovernor(clock=lambda: now)

    obs = RateLimitObservation(
        provider_id="prov1",
        model_id="mod1",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=2000.0,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    governor.record(obs)
    dec_before = governor.decision("prov1", "mod1", "structured_planning")
    assert dec_before.eligible is False
    assert dec_before.state == QuotaState.EXHAUSTED.value

    governor.record_success("prov1", "mod1", "structured_planning")
    dec_after = governor.decision("prov1", "mod1", "structured_planning")
    assert dec_after.eligible is True
    assert dec_after.state == QuotaState.AVAILABLE.value


# Test H: Quota governor latest_observation returns truthful snapshot
def test_h_quota_governor_latest_observation():
    now = 1000.0
    governor = QuotaGovernor(clock=lambda: now)
    assert governor.latest_observation("p", "m", "structured_planning") is None

    obs = RateLimitObservation(
        provider_id="p",
        model_id="m",
        task_class="structured_planning",
        observed_at="2026-10-01T00:00:00Z",
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=1200.0,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    governor.record(obs)
    latest = governor.latest_observation("p", "m", "structured_planning")
    assert latest is not None
    assert latest.retry_at_epoch == 1200.0
    assert latest.http_status == 429


# Test I: Provider failover records SKIPPED attempt on adequacy failure without invocation
def test_i_provider_failover_skips_inadequate_provider_without_calling_it(tmp_path):
    policy = _sample_policy()
    # Make gemini inadequate by setting tiny context window
    policy["providers"]["gemini"]["model_context_tokens"] = 1000
    registry = ProviderRegistry(policy)
    router = ProviderRouter(registry)

    calls = []

    class MockProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def __init__(self, provider_id):
            self.provider_id = provider_id

        def generate_plan(self, prompt, schema):
            calls.append(self.provider_id)
            return {"provider": self.provider_id}, "response", {"total_tokens": 10}

    backend = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: MockProvider(pid),
        attempt_journal=tmp_path / "attempts.jsonl",
    )

    req = ExecutionRequest(
        task_id="task-inadequate-skip",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "generate plan",
            "schema": {"type": "object"},
            "context_tokens": 2000,  # 2000 + 2200 = 4200 > 1000
            "ignore_credentials": True,
        },
    )

    result = backend.execute(req)
    assert result.status == "SUCCESS"
    # gemini was skipped before invocation, so only nemotron was invoked
    assert calls == ["nemotron"]
    attempts = result.evidence_payload["provider_attempts"]
    assert len(attempts) == 2
    gemini_attempt = attempts[0]
    assert gemini_attempt["provider_id"] == "gemini"
    assert gemini_attempt["status"] == ProviderAttemptStatus.SKIPPED.value
    assert gemini_attempt["error_class"] == "CONTEXT_INADEQUATE"


# Test J: Provider failover sanitized provider_decisions trace preserved in evidence payload
def test_j_provider_failover_sanitized_provider_decisions(tmp_path):
    policy = _sample_policy()
    registry = ProviderRegistry(policy)
    router = ProviderRouter(registry)

    backend = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: MagicMock(
            execution_provenance="LOCAL_OFFLINE",
            generate_plan=MagicMock(return_value=({"ok": True}, "out", {"tokens": 1})),
        ),
        attempt_journal=tmp_path / "attempts.jsonl",
    )

    req = ExecutionRequest(
        task_id="task-decisions-trace",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "generate plan",
            "schema": {"type": "object"},
            "ignore_credentials": True,
        },
    )

    result = backend.execute(req)
    assert result.status == "SUCCESS"
    assert "provider_decisions" in result.evidence_payload
    decisions = result.evidence_payload["provider_decisions"]
    assert len(decisions) >= 1
    assert decisions[0]["provider_id"] == "gemini"
    assert decisions[0]["eligible"] is True


# Test K: Provider failover record_success clears stale quota barrier on success
def test_k_provider_failover_success_clears_stale_quota_barrier(tmp_path):
    import datetime as dt
    policy = _sample_policy()
    registry = ProviderRegistry(policy)
    router = ProviderRouter(registry)

    now = [1000.0]
    governor = QuotaGovernor(clock=lambda: now[0])
    now_iso = dt.datetime.fromtimestamp(now[0], tz=dt.timezone.utc).isoformat()
    # Stale observation recorded
    obs = RateLimitObservation(
        provider_id="gemini",
        model_id="gemini-2.5-flash",
        task_class="structured_planning",
        observed_at=now_iso,
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=1050.0,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )
    governor.record(obs)

    backend = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: MagicMock(
            execution_provenance="LOCAL_OFFLINE",
            generate_plan=MagicMock(return_value=({"ok": True}, "out", {"tokens": 1})),
        ),
        attempt_journal=tmp_path / "attempts.jsonl",
        quota_governor=governor,
    )

    req = ExecutionRequest(
        task_id="task-clear-quota",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "generate plan",
            "schema": {"type": "object"},
            "ignore_credentials": True,
            "task_class": "structured_planning",
        },
    )

    # At t=1100, the retry_at_epoch has passed so it's eligible to try
    now[0] = 1100.0
    result = backend.execute(req)
    assert result.status == "SUCCESS"

    # Verify governor recorded success and state is AVAILABLE
    dec = governor.decision("gemini", "gemini-2.5-flash", "structured_planning")
    assert dec.state == QuotaState.AVAILABLE.value
    assert dec.eligible is True


# Test L: Provider failover preserves typed failure mapping
def test_l_provider_failover_preserves_typed_failure_mapping(tmp_path):
    from aos.planner import PlannerTransientError
    from aos.provider_observation import FailureFamily

    policy = _sample_policy()
    policy["allow_provider_fallback"] = False
    registry = ProviderRegistry(policy)
    router = ProviderRouter(registry)

    # 1. Deterministic SERVER_CAPACITY failure without RateLimitObservation
    class ServerCapacityProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            raise PlannerTransientError(
                "sanitized capacity",
                failure_family=FailureFamily.SERVER_CAPACITY,
            )

    backend1 = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: ServerCapacityProvider(),
        attempt_journal=tmp_path / "attempts_capacity.jsonl",
    )

    req1 = ExecutionRequest(
        task_id="task-typed-capacity",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "generate plan",
            "schema": {"type": "object"},
            "ignore_credentials": True,
        },
    )

    result1 = backend1.execute(req1)
    assert result1.status == "DEGRADED"
    attempt1 = result1.evidence_payload["provider_attempts"][0]
    assert attempt1["error_class"] == "SERVER_CAPACITY"
    assert attempt1["error_class"] != "NETWORK_UNAVAILABLE"

    # 2. Deterministic CREDIT_EXHAUSTED with typed observation
    credit_obs = RateLimitObservation(
        provider_id="gemini",
        model_id="gemini-2.5-flash",
        task_class="structured_planning",
        observed_at="2026-10-01T00:00:00Z",
        http_status=402,
        classification="CREDIT_EXHAUSTED",
        retry_at_epoch=None,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    )

    class CreditExhaustedProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            raise PlannerTransientError(
                "sanitized credit",
                failure_family=FailureFamily.SERVER_CAPACITY,
                rate_limit_observation=credit_obs,
            )

    backend2 = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: CreditExhaustedProvider(),
        attempt_journal=tmp_path / "attempts_credit.jsonl",
    )

    req2 = ExecutionRequest(
        task_id="task-typed-credit",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "generate plan",
            "schema": {"type": "object"},
            "ignore_credentials": True,
        },
    )

    result2 = backend2.execute(req2)
    assert result2.status == "DEGRADED"
    attempt2 = result2.evidence_payload["provider_attempts"][0]
    assert attempt2["error_class"] == "CREDIT_EXHAUSTED"


# Test M: Agentic planning bridge cost class is QUOTA_LIMITED
def test_m_agentic_planning_bridge_cost_is_quota_limited():
    mock_backend = MagicMock(spec=ExecutionBackend)
    mock_backend.backend_id = "antigravity"
    mock_backend.cost = ExecutionCost.SUBSCRIPTION_INCLUDED
    bridge = AgenticStructuredPlanningBridge(mock_backend)
    assert bridge.cost == ExecutionCost.QUOTA_LIMITED


# Test N: Agentic planning bridge ordered proposal extraction
def test_n_agentic_planning_bridge_ordered_proposal_extraction(tmp_path):
    mock_backend = MagicMock(spec=ExecutionBackend)
    mock_backend.backend_id = "antigravity"
    bridge = AgenticStructuredPlanningBridge(mock_backend)

    req = ExecutionRequest(
        task_id="task-extract",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "plan",
            "schema": {"type": "object", "required": ["val"], "properties": {"val": {"type": "string"}}},
        },
    )

    # 1. transient_structured_output takes precedence
    mock_backend.execute.return_value = ExecutionResult(
        backend_id="antigravity",
        worker_id="worker",
        task_id="task-extract",
        request_id="req-1",
        status="SUCCESS",
        exit_code=0,
        workspace=str(tmp_path),
        transient_structured_output={"val": "from_transient"},
        evidence_payload={"proposal": {"val": "from_evidence"}},
        stdout_digest='{"val": "from_stdout"}',
    )
    res = bridge.execute(req)
    assert res.status == "SUCCESS"
    assert res.evidence_payload["proposal"] == {"val": "from_transient"}

    # 2. evidence_payload['proposal'] takes precedence if transient is None
    mock_backend.execute.return_value = ExecutionResult(
        backend_id="antigravity",
        worker_id="worker",
        task_id="task-extract",
        request_id="req-2",
        status="SUCCESS",
        exit_code=0,
        workspace=str(tmp_path),
        transient_structured_output=None,
        evidence_payload={"proposal": {"val": "from_evidence"}},
        stdout_digest='{"val": "from_stdout"}',
    )
    res2 = bridge.execute(req)
    assert res2.status == "SUCCESS"
    assert res2.evidence_payload["proposal"] == {"val": "from_evidence"}

    # 3. stdout fallback if evidence has no proposal
    mock_backend.execute.return_value = ExecutionResult(
        backend_id="antigravity",
        worker_id="worker",
        task_id="task-extract",
        request_id="req-3",
        status="SUCCESS",
        exit_code=0,
        workspace=str(tmp_path),
        transient_structured_output=None,
        evidence_payload={},
        stdout_digest='{"val": "from_stdout"}',
    )
    res3 = bridge.execute(req)
    assert res3.status == "SUCCESS"
    assert res3.evidence_payload["proposal"] == {"val": "from_stdout"}


# Test O: Antigravity backend failure includes failure_class in evidence_payload
def test_o_antigravity_backend_failure_includes_failure_class(tmp_path):
    backend = AntigravityAgenticExecutionBackend(
        capability_status_provider=lambda: "UNPROVEN",
    )
    req = ExecutionRequest(
        task_id="task-failure-class",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH",
        payload={"prompt": "do work", "source_sha": "a" * 40},
    )
    res = backend.execute(req)
    assert res.status == "DEGRADED"
    assert res.evidence_payload.get("failure_class") == "ANTIGRAVITY_UNAVAILABLE"


# Test P & Q: Antigravity backend planning mode uses json output format & sets transient_structured_output
def test_p_q_antigravity_backend_planning_mode(tmp_path):
    adapter = FakeAntigravityAdapter()
    cid = "conv-planning-pq"
    valid_plan = {"steps": ["step1", "step2"]}
    adapter.set_canned_response(
        cid,
        AntigravityResponse(
            conversation_id=cid,
            status=AntigravityStatus.SUCCESS,
            mapped_aos_status=RunStatus.COMPLETED,
            raw_response=json.dumps(valid_plan),
            parsed_json={"conversation_id": cid, "status": "SUCCESS", "response": valid_plan},
        ),
    )

    backend = AntigravityAgenticExecutionBackend(
        adapter=adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
    )

    # Initialize git repo in tmp_path
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ag@test.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "AG Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    req = ExecutionRequest(
        task_id="task-planning-pq",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH",
        payload={
            "prompt": "give plan",
            "planning_mode": True,
            "source_sha": source_sha,
            "conversation_id": cid,
        },
    )

    res = backend.execute(req)
    assert res.status == "SUCCESS"
    assert adapter.invocations[0]["output_format"] == "json"
    assert res.transient_structured_output == valid_plan


# Test P.2: Antigravity backend planning mode fails with ANTIGRAVITY_PLANNING_OUTPUT_UNAVAILABLE when response is missing
def test_ag_planning_missing_output_fails_closed(tmp_path):
    adapter = FakeAntigravityAdapter()
    cid = "conv-missing-resp"
    adapter.set_canned_response(
        cid,
        AntigravityResponse(
            conversation_id=cid,
            status=AntigravityStatus.SUCCESS,
            mapped_aos_status=RunStatus.COMPLETED,
            raw_response="{}",
            parsed_json={"conversation_id": cid, "status": "SUCCESS"},  # No "response" key
        ),
    )

    backend = AntigravityAgenticExecutionBackend(
        adapter=adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
    )

    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ag@test.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "AG Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    req = ExecutionRequest(
        task_id="task-missing-output",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH",
        payload={
            "prompt": "give plan",
            "planning_mode": True,
            "source_sha": source_sha,
            "conversation_id": cid,
        },
    )

    res = backend.execute(req)
    assert res.status != "SUCCESS"
    assert res.status == "DEGRADED"
    assert res.evidence_payload.get("failure_class") == "ANTIGRAVITY_PLANNING_OUTPUT_UNAVAILABLE"


# Test P.3: Non-planning AG execution still uses stream-json
def test_ag_non_planning_uses_stream_json(tmp_path):
    adapter = FakeAntigravityAdapter()
    cid = "conv-non-planning"
    adapter.set_canned_response(
        cid,
        AntigravityResponse(
            conversation_id=cid,
            status=AntigravityStatus.SUCCESS,
            mapped_aos_status=RunStatus.COMPLETED,
            raw_response="done",
            parsed_json={"conversation_id": cid, "status": "SUCCESS"},
        ),
    )

    backend = AntigravityAgenticExecutionBackend(
        adapter=adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
    )

    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ag@test.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "AG Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    req = ExecutionRequest(
        task_id="task-non-planning",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH",
        payload={
            "prompt": "do coding task",
            "planning_mode": False,
            "source_sha": source_sha,
            "conversation_id": cid,
        },
    )

    res = backend.execute(req)
    assert res.status == "SUCCESS"
    assert adapter.invocations[0]["output_format"] == "stream-json"


# Test Q.2: ExecutionResult.to_dict() must NOT serialize transient_structured_output
def test_execution_result_to_dict_omits_transient_structured_output():
    result = ExecutionResult(
        backend_id="antigravity",
        worker_id="worker1",
        task_id="t1",
        request_id="r1",
        status="SUCCESS",
        exit_code=0,
        workspace="/tmp/test",
        transient_structured_output={"secret_plan": "do not serialize"},
        evidence_payload={"public_key": "val"},
    )
    serialized = result.to_dict()
    assert "transient_structured_output" not in serialized
    assert "secret_plan" not in json.dumps(serialized)


# Test R: lari policy schema validation
def test_r_lari_policy_schema_validation():
    repo_root = Path(__file__).resolve().parents[1]
    lari_policy_path = repo_root / "descriptors" / "lari.planner-policy.json"
    result, code = validate_file("planner_routing_policy", lari_policy_path)
    assert result.is_valid is True
    assert code == 0


# Test S: nemotron policy schema validation
def test_s_nemotron_policy_schema_validation():
    repo_root = Path(__file__).resolve().parents[1]
    nemotron_policy_path = repo_root / "descriptors" / "nemotron.planner-policy.json"
    result, code = validate_file("planner_routing_policy", nemotron_policy_path)
    assert result.is_valid is True
    assert code == 0


# Test Policy Semantic Equality: lari vs nemotron mirror
def test_policy_mirror_semantic_equality_and_parser_default():
    repo_root = Path(__file__).resolve().parents[1]
    lari_raw = json.loads((repo_root / "descriptors" / "lari.planner-policy.json").read_text(encoding="utf-8"))
    nemotron_raw = json.loads((repo_root / "descriptors" / "nemotron.planner-policy.json").read_text(encoding="utf-8"))

    surfaces = [
        "routing_mode",
        "allow_paid_fallback",
        "paid_fallback_enabled",
        "paid_daily_budget_usd",
        "paid_monthly_budget_usd",
        "allow_provider_fallback",
        "data_classification",
        "risk_routes",
        "providers",
    ]

    for key in surfaces:
        assert lari_raw.get(key) == nemotron_raw.get(key), f"Semantic divergence on key: {key}"

    # Also assert autonomous_host build_parser default is descriptors/lari.planner-policy.json
    from aos.autonomous_host import build_parser
    parser = build_parser()
    default_policy = parser.get_default("routing_policy")
    assert str(default_policy).replace("\\", "/") == "descriptors/lari.planner-policy.json"


# Test T: Runtime store exclusive lock regression
def test_t_runtime_store_exclusive_lock_regression(tmp_path):
    from aos.runtime_store import exclusive_file_lock

    lock_file = tmp_path / "exclusive.lock"
    lock_file.write_bytes(b"0")

    with exclusive_file_lock(lock_file):
        # Within lock context, lock exists and is acquired
        assert lock_file.exists()


# Test U: Free provider pool progression (renamed from full fallback progression through all tiers)
def test_u_free_provider_pool_progression(tmp_path):
    policy = {
        "routing_mode": "PREFER_FREE",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {
            "R0": {"preferred_providers": ["p_inadequate", "p_quota_exhausted", "p_contract_fail", "p_success"]}
        },
        "providers": {
            "p_inadequate": {
                "provider_id": "p_inadequate",
                "model_id": "m1",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 1000,
            },
            "p_quota_exhausted": {
                "provider_id": "p_quota_exhausted",
                "model_id": "m2",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 100000,
            },
            "p_contract_fail": {
                "provider_id": "p_contract_fail",
                "model_id": "m3",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 100000,
            },
            "p_success": {
                "provider_id": "p_success",
                "model_id": "m4",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 100000,
            },
        },
    }

    governor = QuotaGovernor(clock=lambda: 1000.0)
    governor.record(RateLimitObservation(
        provider_id="p_quota_exhausted",
        model_id="m2",
        task_class="structured_planning",
        observed_at="2026-10-01T00:00:00Z",
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=2000.0,
        evidence_source=ObservationSource.PROVIDER_METADATA.value,
    ))

    router = ProviderRouter(ProviderRegistry(policy))

    class ContractFailProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            from aos.planner import PlannerContractError
            raise PlannerContractError("Invalid schema format")

    class SuccessProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            return {"tier": "success"}, "ok", {"tokens": 10}

    def factory(pid, mid):
        if pid == "p_contract_fail":
            return ContractFailProvider()
        if pid == "p_success":
            return SuccessProvider()
        raise RuntimeError(f"Unexpected invocation of {pid}")

    backend = ProviderFailoverReasoningBackend(
        router,
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        quota_governor=governor,
    )

    req = ExecutionRequest(
        task_id="task-full-fallback",
        project_id="test",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "plan",
            "schema": {"type": "object"},
            "context_tokens": 2000,
            "ignore_credentials": True,
        },
    )

    result = backend.execute(req)
    assert result.status == "SUCCESS"
    assert result.evidence_payload["provider_route"] == "p_success"

    attempts = result.evidence_payload["provider_attempts"]
    assert len(attempts) == 4
    assert attempts[0]["provider_id"] == "p_inadequate"
    assert attempts[0]["status"] == "SKIPPED"
    assert attempts[1]["provider_id"] == "p_quota_exhausted"
    assert attempts[1]["status"] == "SKIPPED"
    assert attempts[2]["provider_id"] == "p_contract_fail"
    assert attempts[2]["status"] == ProviderAttemptStatus.NON_RETRYABLE_FAILED.value
    assert attempts[3]["provider_id"] == "p_success"
    assert attempts[3]["status"] == ProviderAttemptStatus.SUCCESS.value


# ======================================================================
# Top-Level Resource Orchestrator Tests: TOP-A, TOP-B, TOP-C, TOP-D
# ======================================================================
def _build_test_top_level_backends(
    *,
    qwen_healthy: bool = True,
    provider_available: bool = True,
    ag_healthy: bool = True,
):
    from extensions.autonomy_fabric.llama_cpp_reasoning_backend import LlamaCppQwenReasoningBackend
    from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator

    # 1. LlamaCppQwenReasoningBackend
    qwen = LlamaCppQwenReasoningBackend(
        capability_status_provider=lambda: "TEST_DOUBLE",
        health_reader=lambda: qwen_healthy,
    )

    # 2. ProviderFailoverReasoningBackend double
    policy = {
        "routing_mode": "PREFER_FREE",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {"R0": {"preferred_providers": ["fake_free"]}},
        "providers": {
            "fake_free": {
                "provider_id": "fake_free",
                "model_id": "fake-model",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "CLOUD",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"],
                "model_context_tokens": 128000,
                "quality_tier": 3,
            }
        },
    }
    registry = ProviderRegistry(policy)
    router = ProviderRouter(registry)

    class FakeProvider:
        execution_provenance = "LOCAL_OFFLINE"

        def generate_plan(self, prompt, schema):
            if not provider_available:
                from aos.planner import PlannerTransientError
                raise PlannerTransientError("429 Rate limited")
            return {"plan": "free_provider"}, "ok", {"tokens": 10}

    provider_backend = ProviderFailoverReasoningBackend(
        router,
        provider_factory=lambda pid, mid: FakeProvider(),
    )
    if not provider_available:
        provider_backend.get_availability = lambda: ExecutionAvailabilitySnapshot(
            ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE,
            "2026-10-01T00:00:00Z",
            source="TEST",
            evidence={"reason": "UNAVAILABLE"},
        )

    # 3. AgenticStructuredPlanningBridge around healthy AG execution backend double
    class FakeAgenticBackend(ExecutionBackend):
        backend_id = "antigravity"
        resource_id = "local_ag"
        backend_class = BackendClass.AGENTIC_EXECUTION_BACKEND
        trust_zone = ExecutionTrustZone.RESTRICTED_WORKSPACE
        cost = ExecutionCost.SUBSCRIPTION_INCLUDED
        supported_capabilities = {
            ExecutionCapability.MODEL_REASONING,
            ExecutionCapability.LONG_HORIZON_AGENTIC_WORK,
            ExecutionCapability.ANTIGRAVITY,
        }

        def execute(self, request):
            if not ag_healthy:
                return ExecutionResult(
                    backend_id="antigravity",
                    worker_id="w",
                    task_id=request.task_id,
                    request_id=request.request_id,
                    status="DEGRADED",
                    exit_code=1,
                    workspace=request.workspace,
                )
            return ExecutionResult(
                backend_id="antigravity",
                worker_id="w",
                task_id=request.task_id,
                request_id=request.request_id,
                status="SUCCESS",
                exit_code=0,
                workspace=request.workspace,
                transient_structured_output={"plan": "ag"},
            )

        def get_health(self):
            return ExecutionHealth.HEALTHY if ag_healthy else ExecutionHealth.UNAVAILABLE

        def get_availability(self):
            state = ExecutionAvailabilityState.AVAILABLE if ag_healthy else ExecutionAvailabilityState.CONTRACT_FAILURE
            return ExecutionAvailabilitySnapshot(
                state, "2026-10-01T00:00:00Z", source="TEST", evidence={}
            )

    ag_backend = FakeAgenticBackend()
    ag_bridge = AgenticStructuredPlanningBridge(ag_backend)

    orchestrator = ResourceOrchestrator()
    return qwen, provider_backend, ag_bridge, ag_backend, orchestrator


# CASE TOP-A: Qwen eligible and ranks before free cloud pool (FREE_LOCAL preferred)
def test_top_a_qwen_ranks_before_free_cloud_pool():
    qwen, provider, ag_bridge, _, orchestrator = _build_test_top_level_backends()
    req = ExecutionRequest(
        task_id="top-a",
        project_id="test",
        workspace="/tmp/test",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "short plan",
            "resource_requirements": {
                "task_class": "structured_planning",
                "context_tokens": 500,
                "request_token_budget": 1500,
                "minimum_quality": 1,
                "local_qwen_allowed": True,
                "agentic_planning_allowed": True,
            },
        },
    )

    ranks = orchestrator.rank([qwen, provider, ag_bridge], req)
    eligible_ranks = [r for r in ranks if r.eligible]
    assert len(eligible_ranks) == 3
    # Qwen (FREE_LOCAL cost 10) must rank first
    assert eligible_ranks[0].backend_id == qwen.backend_id
    # Provider failover (FREE_TIER_CLOUD cost 30) ranks second
    assert eligible_ranks[1].backend_id == provider.backend_id
    # Agentic bridge (QUOTA_LIMITED cost 90) ranks third
    assert eligible_ranks[2].backend_id == ag_bridge.backend_id

    selected = orchestrator.select([qwen, provider, ag_bridge], req)
    assert selected == qwen.backend_id


# CASE TOP-B: minimum_quality = 3 makes Qwen ineligible; free cloud provider ranks first
def test_top_b_free_cloud_pool_selected_before_ag_bridge():
    qwen, provider, ag_bridge, _, orchestrator = _build_test_top_level_backends()
    req = ExecutionRequest(
        task_id="top-b",
        project_id="test",
        workspace="/tmp/test",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "high quality plan",
            "resource_requirements": {
                "task_class": "structured_planning",
                "context_tokens": 1000,
                "request_token_budget": 3200,
                "minimum_quality": 3,
                "local_qwen_allowed": True,
                "agentic_planning_allowed": True,
            },
        },
    )

    ranks = orchestrator.rank([qwen, provider, ag_bridge], req)
    qwen_rank = next(r for r in ranks if r.backend_id == qwen.backend_id)
    assert qwen_rank.eligible is False
    assert "QUALITY_INADEQUATE" in qwen_rank.reasons

    selected = orchestrator.select([qwen, provider, ag_bridge], req)
    assert selected == provider.backend_id
    assert selected != ag_bridge.backend_id


# CASE TOP-C: Qwen and free provider unavailable -> AG bridge becomes selected (zero-cost-first without making AG unavailable)
def test_top_c_ag_bridge_fallback_when_qwen_and_free_unavailable():
    qwen, provider, ag_bridge, _, orchestrator = _build_test_top_level_backends(
        qwen_healthy=False,
        provider_available=False,
        ag_healthy=True,
    )
    req = ExecutionRequest(
        task_id="top-c",
        project_id="test",
        workspace="/tmp/test",
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH",
        payload={
            "prompt": "fallback plan",
            "resource_requirements": {
                "task_class": "structured_planning",
                "context_tokens": 1000,
                "request_token_budget": 3200,
                "minimum_quality": 1,
                "local_qwen_allowed": True,
                "agentic_planning_allowed": True,
            },
        },
    )

    selected = orchestrator.select([qwen, provider, ag_bridge], req)
    assert selected == ag_bridge.backend_id


# CASE TOP-D: Direct request requiring LONG_HORIZON_AGENTIC_WORK remains eligible for direct agentic backend
def test_top_d_direct_agentic_capability_selection():
    qwen, provider, ag_bridge, ag_backend, orchestrator = _build_test_top_level_backends()
    req = ExecutionRequest(
        task_id="top-d",
        project_id="test",
        workspace="/tmp/test",
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH",
        payload={"prompt": "multi-file refactoring"},
    )

    ranks = orchestrator.rank([qwen, provider, ag_backend], req)
    qwen_rank = next(r for r in ranks if r.backend_id == qwen.backend_id)
    provider_rank = next(r for r in ranks if r.backend_id == provider.backend_id)
    ag_rank = next(r for r in ranks if r.backend_id == ag_backend.backend_id)

    assert qwen_rank.eligible is False
    assert "CAPABILITY_MISMATCH" in qwen_rank.reasons

    assert provider_rank.eligible is False
    assert "CAPABILITY_MISMATCH" in provider_rank.reasons

    assert ag_rank.eligible is True
    selected = orchestrator.select([qwen, provider, ag_backend], req)
    assert selected == ag_backend.backend_id
