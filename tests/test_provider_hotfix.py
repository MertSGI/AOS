import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import aos.runtime_server as runtime_server
from aos.control_panel import _HTML, _Handler, _command_work
from aos.controller_relay import ControllerRelayPublisher
from aos.provider_circuit import CircuitState, ProviderCircuitBreakerRegistry
from aos.provider_probe import PROBE_PROMPT
from aos.providers import GeminiPlannerProvider, GroqPlannerProvider, NemotronPlannerProvider
from aos.runtime_server import RuntimeEngine
from aos.runtime_supervisor import RuntimeSupervisor


def _policy(path: Path) -> Path:
    path.write_text(json.dumps({
        "providers": {
            provider_id: {
                "provider_id": provider_id,
                "enabled": True,
                "cloud_local": "LOCAL" if provider_id == "ollama" else "CLOUD",
                "model_id": f"{provider_id}-model",
            }
            for provider_id in ("nemotron", "gemini", "groq", "ollama")
        }
    }), encoding="utf-8")
    return path


def _config(tmp_path: Path, policy: Path) -> dict:
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "runtime"),
        "runtime_token_path": str(tmp_path / "token"),
        "authorized_roots": [str(tmp_path)],
        "projects": {
            "lari": {
                "project_id": "lari",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "lari",
        "production": "NO_GO",
        "ag_backend_enabled": False,
    }


def _command(engine: RuntimeEngine, command_id: str, policy: Path, state: str = "WAITING_FOR_REASONING_PROVIDER") -> Path:
    engine.store.create_command({
        "command_id": command_id,
        "goal": "Continue product execution",
        "project": {
            "project_id": "lari",
            "descriptor_path": str(policy.parent / "descriptor.json"),
            "workspace": str(policy.parent / "workspace"),
            "routing_policy_path": str(policy),
            "standing_authority": True,
        },
        "continuous": True,
    })
    engine.store.write_state(command_id, state=state, attempts=7, completed_batch_count=25)
    return engine.store.command_dir(command_id) / "project-runtime" / "provider-circuits.json"


def test_runtime_health_reads_exact_command_local_registry(tmp_path, monkeypatch):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        circuit_path = _command(engine, "continue-lineage", policy)
        registry = ProviderCircuitBreakerRegistry(circuit_path)
        registry.record_success("gemini", observed_at="2026-09-19T10:00:00+00:00")
        monkeypatch.setattr(runtime_server, "provider_presence", lambda: {"GEMINI": True})
        health = engine.health()
        assert health["provider_circuit_registry_paths"] == [str(circuit_path)]
        assert health["healthy_reasoning_providers"] == ["gemini"]
        assert health["healthy_reasoning_provider_count"] == 1
    finally:
        engine.shutdown()


def test_cloud_probe_is_live_external_and_preserves_required_nullable_field():
    assert "target_base_sha=0000000000000000000000000000000000000000" in PROBE_PROMPT
    assert NemotronPlannerProvider.execution_provenance == "LIVE_EXTERNAL"
    assert GeminiPlannerProvider.execution_provenance == "LIVE_EXTERNAL"
    assert GroqPlannerProvider.execution_provenance == "LIVE_EXTERNAL"


def test_new_enabled_provider_is_unknown_and_credential_is_not_health(tmp_path, monkeypatch):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        _command(engine, "continue-unknown", policy)
        monkeypatch.setattr(runtime_server, "provider_presence", lambda: {
            "NVIDIA": True, "GEMINI": True, "GROQ": True,
        })
        health = engine.health()
        assert health["healthy_reasoning_provider_count"] == 0
        assert health["unknown_reasoning_provider_count"] == 4
        assert {row["circuit_state"] for row in health["provider_details"]} == {"UNKNOWN"}
    finally:
        engine.shutdown()


def test_most_recent_authoritative_observation_wins(tmp_path):
    first = ProviderCircuitBreakerRegistry(tmp_path / "first.json")
    second = ProviderCircuitBreakerRegistry(tmp_path / "second.json")
    first.record_success("nemotron", observed_at="2026-09-19T10:00:00+00:00")
    second.record_failure(
        "nemotron", "RATE_LIMITED", now=2000,
        observed_at="2026-09-19T10:01:00+00:00",
    )
    merged = ProviderCircuitBreakerRegistry.aggregate_registries([first, second], now=2001)
    assert merged.get_circuit("nemotron").circuit_state == CircuitState.OPEN.value
    assert merged.get_circuit("nemotron").last_failure_class == "RATE_LIMITED"

    first.record_success("nemotron", observed_at="2026-09-19T10:02:00+00:00")
    recovered = ProviderCircuitBreakerRegistry.aggregate_registries([first, second], now=2001)
    assert recovered.get_circuit("nemotron").circuit_state == CircuitState.CLOSED.value


def test_waiting_command_exposes_next_probe_and_probe_does_not_increment_worker_attempts(tmp_path, monkeypatch):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        command_id = "continue-preserved"
        circuit_path = _command(engine, command_id, policy)
        before = engine.store.read_state(command_id)["attempts"]
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda _path: {
            "gemini": {
                "provider_id": "gemini", "credential_available": True,
                "local_service_available": None, "probe_attempted": True,
                "probe_status": "FAIL", "failure_class": "RATE_LIMITED",
                "last_observed_at": "2026-09-19T10:00:00+00:00", "latency_ms": 12,
            }
        })
        engine._run_live_probe_cycle()
        health = engine.health()
        assert engine.store.read_state(command_id)["attempts"] == before
        assert health["next_provider_probe_at"] is not None
        assert ProviderCircuitBreakerRegistry(circuit_path).get_circuit("gemini").probe_count == 1
    finally:
        engine.shutdown()


def test_healthy_alternate_wakes_same_command_lineage(tmp_path, monkeypatch):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        command_id = "continue-b181ddc574c25c2aa0f2a6b9"
        _command(engine, command_id, policy)
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda _path: {
            "groq": {
                "provider_id": "groq", "credential_available": True,
                "local_service_available": None, "probe_attempted": True,
                "probe_status": "PASS", "failure_class": None,
                "last_observed_at": "2026-09-19T10:00:00+00:00", "latency_ms": 8,
            }
        })
        engine._run_live_probe_cycle()
        state = engine.store.read_state(command_id)
        assert state["command_id"] == command_id
        assert state["retry_after_epoch"] == 0
        assert state["attempts"] == 7
    finally:
        engine.shutdown()


def test_newer_success_in_active_lineage_wakes_waiting_lineage(tmp_path):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        waiting = "continue-waiting-lineage"
        active = "continue-active-lineage"
        waiting_circuits = _command(engine, waiting, policy)
        active_circuits = _command(engine, active, policy, state="RUNNING")
        engine.store.write_state(waiting, retry_after_epoch=time.time() + 900)
        registry = ProviderCircuitBreakerRegistry(active_circuits)
        registry.record_success("nemotron", observed_at="2026-09-19T10:01:00+00:00")

        engine._wake_waiting_from_observed_provider_health()

        state = engine.store.read_state(waiting)
        assert state["retry_after_epoch"] == 0
        assert state["command_id"] == waiting
        events_path = engine.store.command_dir(waiting) / "events.jsonl"
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
        wake = [event for event in events if event["event_type"] == "provider.healthy_alternate_wake"][-1]
        assert wake["payload"]["healthy_providers"] == ["nemotron"]
        assert wake["payload"]["evidence_source"] == "NEWEST_COMMAND_LOCAL_OBSERVATION"
        propagated = ProviderCircuitBreakerRegistry(waiting_circuits).get_circuit("nemotron")
        assert propagated.circuit_state == CircuitState.CLOSED.value
        assert propagated.last_success_at == "2026-09-19T10:01:00+00:00"
    finally:
        engine.shutdown()


def test_relay_provider_telemetry_equals_runtime_evidence(tmp_path):
    health = {
        "runtime_state": "HEALTHY",
        "healthy_reasoning_provider_count": 1,
        "probe_eligible_reasoning_provider_count": 1,
        "unknown_reasoning_provider_count": 1,
        "provider_circuits_open": 2,
        "provider_probe_count": 9,
        "provider_failover_count": 3,
        "current_selected_reasoning_provider": "groq",
        "provider_details": [{"provider_id": "groq", "circuit_state": "CLOSED"}],
    }
    relay = ControllerRelayPublisher(tmp_path / "relay", {"runtime_root": str(tmp_path / "state")})
    snapshot = relay.collect_snapshot(runtime_health_dict=health)
    assert snapshot.provider_details == health["provider_details"]
    assert snapshot.current_selected_reasoning_provider == "groq"
    assert snapshot.probe_eligible_reasoning_provider_count == 1
    assert snapshot.unknown_reasoning_provider_count == 1


def test_panel_renders_truthful_provider_and_current_work_fields(tmp_path):
    root = tmp_path / "command"
    runtime = root / "project-runtime"
    runtime.mkdir(parents=True)
    (runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
        "phase": "EXECUTING",
        "batch_number": 26,
        "objective": {"title": "Ship responsive product flow"},
        "last_receipt": {"completed_task_ids": ["task-test"]},
        "completed_batches": [{"receipt": {"timestamp": "2026-09-19T10:00:00+00:00"}}],
    }), encoding="utf-8")
    situation = runtime / "situation-0026.json"
    situation.write_text(json.dumps({"canonical_next_action": "Run browser acceptance", "ci_state": "PASS"}), encoding="utf-8")
    plan_dir = runtime / "batches" / "batch-0026"
    plan_dir.mkdir(parents=True)
    (plan_dir / "generated-run-plan.json").write_text(json.dumps({
        "tasks": [{"node_id": "task-test"}, {"node_id": "task-browser"}]
    }), encoding="utf-8")
    work = _command_work(root, {"state": "RUNNING"}, {"goal": "Continue"})
    assert work["current_objective"] == "Ship responsive product flow"
    assert work["current_task"] == "task-browser"
    assert work["canonical_next_action"] == "Run browser acceptance"
    assert "Provider</th>" in _HTML
    assert "Current Task:" in _HTML
    assert "All health invariants normal" not in _HTML


def test_panel_is_reachable_without_ag_and_binds_loopback_only(tmp_path, monkeypatch):
    monkeypatch.setenv("AOS_PANEL_SUPERVISOR_PID", "4242")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.aos_config = {"runtime_root": str(tmp_path), "default_project": {}}  # type: ignore[attr-defined]
    server.aos_token = "x" * 40  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_address[0] == "127.0.0.1"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/health", timeout=3
        ) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert health["panel_state"] == "HEALTHY"
        assert health["owner"] == "AOS"
        assert health["ag_required"] is False
        assert health["supervisor_pid"] == "4242"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_supervisor_restarts_failed_panel_companion(tmp_path, monkeypatch):
    config_path = tmp_path / "supervisor-config.json"
    config_path.write_text(json.dumps({
        "supervisor_root": str(tmp_path / "supervisor"),
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "panel_enabled": True,
    }), encoding="utf-8")
    supervisor = RuntimeSupervisor(config_path)
    host_config = tmp_path / "host.json"
    panel_config = tmp_path / "panel.json"
    host_config.write_text("{}", encoding="utf-8")
    panel_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(supervisor, "_panel_health", lambda: None)
    monkeypatch.setattr(supervisor, "_ensure_panel_configs", lambda: (host_config, panel_config))
    launched = []

    class DeadPanel:
        pid = 999
        def poll(self):
            return 1

    monkeypatch.setattr(
        "aos.runtime_supervisor.popen_headless",
        lambda command, **kwargs: launched.append(command) or DeadPanel(),
    )
    assert supervisor._ensure_panel("a" * 40) is False
    assert supervisor._ensure_panel("a" * 40) is False
    assert len(launched) == 2
    assert all("aos.control_panel" in command for command in launched)
