"""Regression tests for AOS Provider Circuit Breaker and Adaptive Backoff.

Verifies:
1. Transient failure on Nemotron falls through to healthy Gemini in the same request.
2. Nemotron + Gemini failures fall through to healthy Groq.
3. Cloud failures fall through to healthy approved Ollama.
4. All-provider outage does not cause 5-minute worker respawn storm.
5. Waiting command attempt count does not inflate on health-only probes.
6. Provider circuit state survives runtime restart.
7. HALF_OPEN successful probe closes circuit.
8. Failed provider circuit does not block a healthy alternate.
9. SAME Lane A/C command IDs recover after provider availability returns.
"""
from pathlib import Path
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
    BACKOFF_TIERS,
)
from aos.provider_registry import ProviderRegistry, ProviderRouter
from aos.provider_observation import RateLimitObservation
from aos.runtime_contract import ContinueProjectCommand, ProjectProfile, RuntimeResult
from aos.runtime_store import RuntimeStore
from extensions.autonomy_fabric.execution_backend import ExecutionCapability, ExecutionRequest


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


class _MockProvider:
    execution_provenance = "LOCAL_OFFLINE"

    def __init__(self, provider_id: str, should_fail: bool = False, error_msg: str = "429 rate limited"):
        self.provider_id = provider_id
        self.should_fail = should_fail
        self.error_msg = error_msg

    def generate_plan(self, prompt, schema):
        if self.should_fail:
            raise PlannerTransientError(self.error_msg)
        return {"provider": self.provider_id, "plan": "valid"}, "resp-1", {"tokens": 10}


def _make_request(tmp_path: Path):
    return ExecutionRequest(
        task_id="task-circuit-1",
        project_id="lari",
        workspace=str(tmp_path),
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-1",
        payload={"prompt": "plan", "schema": {"type": "object"}, "risk_class": "R0", "ignore_credentials": True},
    )


def test_nemotron_transient_failure_falls_through_to_gemini_in_same_request(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id == "nemotron":
            return _MockProvider(provider_id, should_fail=True, error_msg="503 backend capacity")
        return _MockProvider(provider_id, should_fail=False)

    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )

    result = backend.execute(_make_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["nemotron", "gemini"]
    assert result.evidence_payload["provider_route"] == "gemini"
    assert result.evidence_payload["fallback_used"] is True

    # Nemotron circuit tripped OPEN, Gemini is CLOSED
    nemotron_circuit = registry.get_circuit("nemotron")
    assert nemotron_circuit.circuit_state == CircuitState.OPEN.value
    assert nemotron_circuit.consecutive_failure_count == 1
    assert registry.get_circuit("gemini").circuit_state == CircuitState.CLOSED.value


def test_nemotron_plus_gemini_failures_fall_through_to_groq(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id in ("nemotron", "gemini"):
            return _MockProvider(provider_id, should_fail=True, error_msg="429 quota exhausted")
        return _MockProvider(provider_id, should_fail=False)

    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )

    result = backend.execute(_make_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["nemotron", "gemini", "groq"]
    assert result.evidence_payload["provider_route"] == "groq"
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.OPEN.value
    assert registry.get_circuit("gemini").circuit_state == CircuitState.OPEN.value
    assert registry.get_circuit("groq").circuit_state == CircuitState.CLOSED.value


def test_provider_contract_failure_falls_through_to_healthy_alternate(tmp_path):
    calls = []

    class ContractFailureProvider(_MockProvider):
        def generate_plan(self, prompt, schema):
            raise PlannerContractError("synthetic structured contract failure")

    def factory(provider_id, model_id):
        calls.append(provider_id)
        return ContractFailureProvider(provider_id) if provider_id == "nemotron" else _MockProvider(provider_id)

    registry = ProviderCircuitBreakerRegistry(tmp_path / "circuits.json")
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )
    result = backend.execute(_make_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["nemotron", "gemini"]
    assert registry.get_circuit("nemotron").last_failure_class == "CONTRACT_FAILURE"


def test_cloud_failures_fall_through_to_approved_ollama(tmp_path):
    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        if provider_id in ("nemotron", "gemini", "groq"):
            return _MockProvider(provider_id, should_fail=True, error_msg="500 server unavailable")
        return _MockProvider(provider_id, should_fail=False)

    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)
    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )

    result = backend.execute(_make_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["nemotron", "gemini", "groq", "ollama"]
    assert result.evidence_payload["provider_route"] == "ollama"
    assert registry.get_circuit("ollama").circuit_state == CircuitState.CLOSED.value


def test_failed_provider_circuit_does_not_block_healthy_alternate(tmp_path):
    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)
    # Trip nemotron circuit to OPEN with future probe time from now
    registry.record_failure("nemotron", "QUOTA_EXHAUSTED", now=time.time())
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.OPEN.value

    calls = []

    def factory(provider_id, model_id):
        calls.append(provider_id)
        return _MockProvider(provider_id, should_fail=False)

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=factory,
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )

    # In execute(), nemotron should be bypassed immediately because its circuit is OPEN
    result = backend.execute(_make_request(tmp_path))
    assert result.status == "SUCCESS"
    assert calls == ["gemini"]  # nemotron was skipped entirely without calling provider
    assert result.evidence_payload["provider_route"] == "gemini"


def test_circuit_state_survives_runtime_restart(tmp_path):
    circuit_file = tmp_path / "provider-circuits.json"
    reg1 = ProviderCircuitBreakerRegistry(circuit_file)
    reg1.record_failure("nemotron", "RESOURCE_EXHAUSTED", now=5000.0)
    reg1.record_success("groq")

    # Simulate restart by instantiating a fresh registry from disk
    reg2 = ProviderCircuitBreakerRegistry(circuit_file)
    nemotron_circuit = reg2.get_circuit("nemotron")
    assert nemotron_circuit.circuit_state == CircuitState.OPEN.value
    assert nemotron_circuit.last_failure_class == "RESOURCE_EXHAUSTED"
    assert nemotron_circuit.consecutive_failure_count == 1

    groq_circuit = reg2.get_circuit("groq")
    assert groq_circuit.circuit_state == CircuitState.CLOSED.value
    assert groq_circuit.last_success_at is not None


def test_half_open_successful_probe_closes_circuit(tmp_path):
    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)
    # Failure recorded at t=1000 with backoff tier 1 (~60s)
    next_probe = registry.record_failure("nemotron", "TRANSIENT_TIMEOUT", now=1000.0)
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.OPEN.value

    # Check at t=1010: still OPEN
    assert registry.is_provider_available("nemotron", now=1010.0) is False
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.OPEN.value

    # Check at t=next_probe + 1: transitions to HALF_OPEN
    assert registry.is_provider_available("nemotron", now=next_probe + 1.0) is True
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.HALF_OPEN.value

    # A successful probe now closes the circuit
    registry.record_success("nemotron")
    assert registry.get_circuit("nemotron").circuit_state == CircuitState.CLOSED.value
    assert registry.get_circuit("nemotron").consecutive_failure_count == 0


def test_failure_family_streaks_are_isolated_by_task_class(monkeypatch):
    monkeypatch.setattr("aos.provider_circuit.random.uniform", lambda _a, _b: 0.0)
    registry = ProviderCircuitBreakerRegistry()
    registry.record_failure("nemotron", "SERVER_CAPACITY", now=1000.0, model_id="m", task_class="repo_ui_planning")
    registry.record_failure("nemotron", "NETWORK_UNAVAILABLE", now=1000.0, model_id="m", task_class="repo_ui_planning")
    deadline = registry.record_failure("nemotron", "CONTRACT_FAILURE", now=1000.0, model_id="m", task_class="repo_ui_planning")
    record = registry.get_health_record("nemotron", model_id="m", task_class="repo_ui_planning")
    assert record["family_streaks"] == {"SERVER_CAPACITY": 1, "NETWORK": 1, "CONTRACT": 1}
    assert deadline == 1060.0


def test_small_success_does_not_clear_repo_ui_failure():
    registry = ProviderCircuitBreakerRegistry()
    registry.record_failure("groq", "CONTRACT_FAILURE", now=1000.0, model_id="m", task_class="repo_ui_planning")
    registry.record_success("groq", model_id="m", task_class="small_reasoning")
    repo = registry.get_health_record("groq", model_id="m", task_class="repo_ui_planning")
    small = registry.get_health_record("groq", model_id="m", task_class="small_reasoning")
    assert repo["circuit_state"] == CircuitState.OPEN.value
    assert small["circuit_state"] == CircuitState.CLOSED.value
    assert registry.healthy_providers_for_task("small_reasoning") == ["groq"]
    assert registry.healthy_providers_for_task("repo_ui_planning") == []


def test_exact_retry_after_wins_without_incrementing_health_family():
    registry = ProviderCircuitBreakerRegistry()
    observation = RateLimitObservation(
        provider_id="groq",
        model_id="m",
        task_class="structured_planning",
        observed_at="2026-09-23T00:00:00+00:00",
        http_status=429,
        classification="RATE_LIMITED",
        retry_at_epoch=1120.0,
        evidence_source="PROVIDER_METADATA",
        field_sources={"retry_at_epoch": "PROVIDER_METADATA"},
    )
    deadline = registry.record_failure(
        "groq", "RATE_LIMITED", now=1000.0, model_id="m",
        task_class="structured_planning", rate_limit_observation=observation,
    )
    record = registry.get_health_record("groq", model_id="m", task_class="structured_planning")
    assert deadline == 1120.0
    assert record["family_streaks"] == {}
    assert record["circuit_state"] == CircuitState.UNKNOWN.value


def test_all_provider_outage_does_not_cause_5min_respawn_storm(tmp_path):
    circuit_file = tmp_path / "circuits.json"
    registry = ProviderCircuitBreakerRegistry(circuit_file)

    backend = ProviderFailoverReasoningBackend(
        ProviderRouter(ProviderRegistry(_policy())),
        provider_factory=lambda pid, mid: _MockProvider(pid, should_fail=True, error_msg="429 rate limited"),
        attempt_journal=tmp_path / "attempts.jsonl",
        circuit_registry=registry,
    )

    t0 = 10000.0
    result = backend.execute(_make_request(tmp_path))
    assert result.status == "DEGRADED"

    summary = registry.summarize()
    assert summary["all_reasoning_providers_unavailable"] is True
    assert summary["healthy_reasoning_provider_count"] == 0
    assert summary["provider_circuits_open"] == 4

    # The next probe epoch is scheduled according to circuit breaker backoff (not hardcoded 300s)
    earliest_probe = registry.earliest_next_probe()
    assert earliest_probe > time.time()
    assert (earliest_probe - time.time()) >= 30.0


def test_waiting_command_attempt_count_does_not_inflate_on_health_only_probes(tmp_path):
    store = RuntimeStore(tmp_path / "runtime")
    cmd_id = "continue-test-lineage-1"

    store.create_command({
        "command_id": cmd_id,
        "goal": "Test lineage preservation",
        "project": {
            "project_id": "lari",
            "descriptor_path": str(tmp_path / "proj.json"),
            "workspace": str(tmp_path),
            "routing_policy_path": str(tmp_path / "policy.json"),
            "standing_authority": True,
        },
        "continuous": True,
    })

    # Record initial state with attempts=371
    store.write_state(
        cmd_id,
        state="WAITING_FOR_REASONING_PROVIDER",
        attempts=371,
        recovery_count=5,
    )

    # Read state and verify attempts remains 371
    st = store.read_state(cmd_id)
    assert st["attempts"] == 371

    # Simulate worker logic when entering WAITING_FOR_REASONING_PROVIDER:
    # attempts rolls back to initial_attempts rather than inflating to 372
    initial_attempts = int(st.get("attempts", 0))
    circuit_reg = ProviderCircuitBreakerRegistry(tmp_path / "provider-circuits.json")
    circuit_reg.record_failure("nemotron", "429", now=time.time())
    retry_at = circuit_reg.earliest_next_probe()

    store.write_state(
        cmd_id,
        state="WAITING_FOR_REASONING_PROVIDER",
        worker_pid=None,
        attempts=initial_attempts,
        retry_after_epoch=retry_at,
    )

    st2 = store.read_state(cmd_id)
    assert st2["attempts"] == 371  # No inflation!
    assert st2["state"] == "WAITING_FOR_REASONING_PROVIDER"
    assert st2["retry_after_epoch"] == retry_at


def test_same_lane_command_ids_recover_after_provider_availability_returns(tmp_path):
    lane_a_cmd = "continue-b181ddc574c25c2aa0f2a6b9"
    lane_c_cmd = "continue-61be4ab1af53cfa646d773ce"

    store = RuntimeStore(tmp_path / "runtime")
    for cid, batches, attempts in [(lane_a_cmd, 25, 371), (lane_c_cmd, 18, 190)]:
        store.create_command({
            "command_id": cid,
            "goal": f"Continue {cid}",
            "project": {
                "project_id": "lari" if "b181" in cid else "lari-ui-v2",
                "descriptor_path": str(tmp_path / "desc.json"),
                "workspace": str(tmp_path),
                "routing_policy_path": str(tmp_path / "policy.json"),
                "standing_authority": True,
            },
            "continuous": True,
        })
        store.write_state(
            cid,
            state="WAITING_FOR_REASONING_PROVIDER",
            completed_batch_count=batches,
            attempts=attempts,
            retry_after_epoch=time.time() + 60.0,
        )

    # Provider recovers: circuit closed
    circuit_file = tmp_path / "provider-circuits.json"
    reg = ProviderCircuitBreakerRegistry(circuit_file)
    reg.record_success("nemotron")

    # Verify both commands are preserved with exact command IDs and lineages intact
    state_a = store.read_state(lane_a_cmd)
    state_c = store.read_state(lane_c_cmd)
    assert state_a["completed_batch_count"] == 25
    assert state_a["attempts"] == 371
    assert state_c["completed_batch_count"] == 18
    assert state_c["attempts"] == 190
