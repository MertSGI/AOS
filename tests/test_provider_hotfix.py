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
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        circuit_path = _command(engine, "continue-lineage", policy)
        registry = ProviderCircuitBreakerRegistry(circuit_path)
        registry.record_success("gemini", observed_at="2026-09-19T10:00:00+00:00")
        monkeypatch.setattr(runtime_server, "provider_presence", lambda: {"GEMINI": True})
        health = engine._collect_detailed_status()
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
        health = engine._collect_detailed_status()
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
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        command_id = "continue-preserved"
        circuit_path = _command(engine, command_id, policy)
        before = engine.store.read_state(command_id)["attempts"]
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda _path, _providers: {
            "gemini": {
                "provider_id": "gemini", "credential_available": True,
                "local_service_available": None, "probe_attempted": True,
                "probe_status": "FAIL", "failure_class": "RATE_LIMITED",
                "last_observed_at": "2026-09-19T10:00:00+00:00", "latency_ms": 12,
            }
        })
        engine._run_live_probe_cycle()
        health = engine._collect_detailed_status()
        assert engine.store.read_state(command_id)["attempts"] == before
        assert health["next_provider_probe_at"] is not None
        assert ProviderCircuitBreakerRegistry(circuit_path).get_circuit("gemini").probe_count == 1
    finally:
        engine.shutdown()


def test_healthy_alternate_wakes_same_command_lineage(tmp_path, monkeypatch):
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        # This is a synchronous unit proof of one probe cycle. Stop the
        # constructor-started workers so a warm runner cannot consume the due
        # probe or recover the command before the controlled invocation below.
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)
        command_id = "continue-b181ddc574c25c2aa0f2a6b9"
        _command(engine, command_id, policy)
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda _path, _providers: {
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
        # Stop constructor-started workers so background recovery/probes cannot
        # mutate state or race fixture initialization.
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        waiting = "continue-waiting-lineage"
        active = "continue-active-lineage"
        waiting_circuits = _command(engine, waiting, policy)
        active_circuits = _command(engine, active, policy, state="RUNNING")
        engine.store.write_state(waiting, retry_after_epoch=time.time() + 900)
        registry = ProviderCircuitBreakerRegistry(active_circuits)
        registry.record_success(
            "nemotron",
            observed_at="2026-09-19T10:01:00+00:00",
            model_id="nemotron-model",
            task_class="UNKNOWN",
        )

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


def test_provider_wait_woken_command_spawns_worker_not_rehold(tmp_path, monkeypatch):
    """Regression: a waiting-for-provider command explicitly woken by
    _wake_waiting_from_observed_provider_health (retry_after_epoch=0) must
    escape the resource_wait_held cycle and spawn a worker."""
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        command_id = "continue-wake-resume"
        _command(engine, command_id, policy)

        project_runtime = engine.store.command_dir(command_id) / "project-runtime"
        project_runtime.mkdir(parents=True, exist_ok=True)
        (project_runtime / "planning-kernel-checkpoint.json").write_text(json.dumps({
            "phase": "WAITING_FOR_REASONING_PROVIDER",
            "batch_number": 25,
            "completed_batch_count": 25,
        }), encoding="utf-8")

        monkeypatch.setattr("aos.runtime_server.pid_alive", lambda _pid: False)
        calls = []
        monkeypatch.setattr(
            engine, "_spawn_worker",
            lambda cid, recovered: calls.append((cid, recovered)) or 123,
        )

        # Calls 1, 2: normal resume + strategy escalation → worker spawned
        engine._recover_one(command_id)
        engine._recover_one(command_id)
        assert len(calls) == 2

        # Call 3: same_fingerprint_respawns >= 3 and is_provider_wait=True
        # → resource_wait_held, retry_after_epoch set to future, no spawn
        engine._recover_one(command_id)
        state_held = engine.store.read_state(command_id)
        assert state_held["state"] == "WAITING_FOR_REASONING_PROVIDER"
        assert state_held["recovery_disposition"] == "WAITING_FOR_RESOURCE"
        assert float(state_held["retry_after_epoch"]) > 0
        assert len(calls) == 2  # No new spawn

        # Simulate _wake_waiting_from_observed_provider_health clearing retry
        engine.store.write_state(command_id, retry_after_epoch=0)

        # Call 4: with retry_after_epoch=0 and prior recovery_disposition=WAITING_FOR_RESOURCE
        # the engine must detect the wake signal, reset churn counter, and spawn
        engine._recover_one(command_id)
        state_resumed = engine.store.read_state(command_id)
        assert len(calls) == 3, f"Expected worker spawn after wake, got {len(calls)} calls"
        assert calls[2] == (command_id, True)
        assert state_resumed["same_fingerprint_respawns"] == 1
        assert state_resumed["recovery_disposition"] == "RESOURCE_WAKE_RESUME"
    finally:
        engine.shutdown()


def test_historical_command_with_stale_old_slot_policy_resolves_current_slot(tmp_path, monkeypatch):
    """Historical command with old-slot policy path resolves current slot policy in server telemetry."""
    old_slot = tmp_path / "old_slot"
    old_descriptors = old_slot / "descriptors"
    old_descriptors.mkdir(parents=True)
    old_policy = old_descriptors / "nemotron.planner-policy.json"
    old_policy.write_text(json.dumps({"providers": {"old_prov": {"enabled": True}}}), encoding="utf-8")

    current_slot = tmp_path / "current_slot"
    current_descriptors = current_slot / "descriptors"
    current_descriptors.mkdir(parents=True)
    current_policy = current_descriptors / "nemotron.planner-policy.json"
    current_policy.write_text(json.dumps({"providers": {"gemini": {"enabled": True, "billing_class": "FREE"}}}), encoding="utf-8")

    cfg = _config(tmp_path, current_policy)
    cfg["runtime_slot_root"] = str(current_slot)
    monkeypatch.setenv("AOS_RUNTIME_SLOT_ROOT", str(current_slot))

    # Clear class-level policy caches to prevent cross-test pollution
    with RuntimeEngine._policy_cache_lock:
        RuntimeEngine._policy_cache.clear()
        RuntimeEngine._policy_error.clear()

    engine = RuntimeEngine(cfg)
    try:
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        # Command points to the old_policy path
        _command(engine, "historical-cmd", old_policy)

        status = engine._collect_detailed_status()
        # Telemetry should discover the current slot's policy ("gemini"), not "old_prov"
        assert "gemini" in status["enabled_reasoning_providers"]
        assert "old_prov" not in status["enabled_reasoning_providers"]
        assert status["provider_discovery_status"] == "HEALTHY"

        # Durable command lineage must NOT be rewritten
        durable_cmd = engine.store.read_command("historical-cmd")
        assert durable_cmd["project"]["routing_policy_path"] == str(old_policy)
    finally:
        engine.shutdown()


def test_worker_and_server_resolve_identical_policy_file(tmp_path, monkeypatch):
    """Worker and server resolve the SAME effective policy file for an old-slot command."""
    from aos.runtime_worker import _active_runtime_artifact_path
    from aos.runtime_assets import resolve_active_runtime_artifact

    slot = tmp_path / "slot"
    descriptors = slot / "descriptors"
    descriptors.mkdir(parents=True)
    active_policy = descriptors / "nemotron.planner-policy.json"
    active_policy.write_text("{}", encoding="utf-8")

    stale_path = str(tmp_path / "historical" / "candidate" / "deadbeef" / "descriptors" / "nemotron.planner-policy.json")

    monkeypatch.setenv("AOS_RUNTIME_SLOT_ROOT", str(slot))

    worker_resolved = _active_runtime_artifact_path(stale_path)
    server_resolved = resolve_active_runtime_artifact(stale_path, slot_root=str(slot))

    assert worker_resolved == active_policy
    assert server_resolved == active_policy
    assert worker_resolved == server_resolved


def test_product_workspace_is_not_rebound(tmp_path, monkeypatch):
    """Product workspace paths MUST NOT be rebound to the slot root."""
    from aos.runtime_assets import resolve_active_runtime_artifact

    slot = tmp_path / "slot"
    (slot / "descriptors").mkdir(parents=True)
    monkeypatch.setenv("AOS_RUNTIME_SLOT_ROOT", str(slot))

    product_ws = tmp_path / "product-repo"
    product_ws.mkdir()

    resolved = resolve_active_runtime_artifact(product_ws, slot_root=str(slot))
    assert resolved == product_ws.resolve()


def test_malformed_policy_yields_degraded_telemetry_and_retains_last_graph(tmp_path):
    """Malformed policy yields DEGRADED, not false empty/healthy telemetry."""
    policy = _policy(tmp_path / "policy.json")
    engine = RuntimeEngine(_config(tmp_path, policy))
    try:
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        _command(engine, "cmd-ok", policy)
        status_ok = engine._collect_detailed_status()
        assert status_ok["provider_discovery_status"] == "HEALTHY"
        assert len(status_ok["enabled_reasoning_providers"]) > 0

        # Now corrupt policy file
        policy.write_text("{invalid json", encoding="utf-8")
        # Clear policy cache to simulate re-read
        with runtime_server.RuntimeEngine._policy_cache_lock:
            runtime_server.RuntimeEngine._policy_cache.clear()

        status_degraded = engine._collect_detailed_status()
        assert status_degraded["provider_discovery_status"] == "DEGRADED"
        assert bool(status_degraded["provider_discovery_errors"])
        # Retains last known accepted provider graph rather than presenting false empty
        assert status_degraded["enabled_reasoning_providers"] == status_ok["enabled_reasoning_providers"]
    finally:
        engine.shutdown()
        # Clean up class-level error state to prevent cross-test pollution
        with runtime_server.RuntimeEngine._policy_cache_lock:
            runtime_server.RuntimeEngine._policy_cache.clear()
            runtime_server.RuntimeEngine._policy_error.clear()


def test_relay_renders_discovery_degradation_and_paths(tmp_path):
    """Controller Relay markdown renders provider discovery status and error details."""
    relay_dir = tmp_path / "relay"
    publisher = ControllerRelayPublisher(relay_dir, {})

    rh = {
        "healthy_reasoning_provider_count": 0,
        "probe_eligible_reasoning_provider_count": 0,
        "unknown_reasoning_provider_count": 0,
        "provider_circuits_open": 0,
        "all_reasoning_providers_unavailable": False,
        "provider_details": [],
        "enabled_reasoning_providers": ["nemotron", "groq"],
        "provider_circuit_registry_paths": ["/path/to/circuits.json"],
        "provider_discovery_status": "DEGRADED",
        "provider_discovery_errors": {"/path/to/policy.json": {"error_class": "JSONDecodeError"}},
    }

    snapshot = publisher.collect_snapshot(runtime_health_dict=rh)
    md = publisher.render_markdown(snapshot)

    assert "PROVIDER_DISCOVERY_STATUS=DEGRADED" in md
    assert "JSONDecodeError" in md
    assert '["nemotron", "groq"]' in md
    assert '["/path/to/circuits.json"]' in md


def test_paid_provider_excluded_under_zero_cost_policy(tmp_path):
    """Paid provider remains excluded under default zero-cost policy."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({
        "allow_paid_fallback": False,
        "paid_fallback_enabled": False,
        "paid_daily_budget_usd": 0.0,
        "providers": {
            "free_one": {"enabled": True, "billing_class": "FREE"},
            "paid_one": {"enabled": True, "billing_class": "PAID"},
        },
    }), encoding="utf-8")

    enabled = runtime_server.RuntimeEngine._enabled_from_policy(policy_file)
    assert "free_one" in enabled
    assert "paid_one" not in enabled


def test_stale_policy_path_nemotron_pass_wakes_protected_lari_lineage(tmp_path, monkeypatch):
    """End-to-end regression: stale-policy-path → Nemotron PASS → protected LARI wake.

    A historical protected LARI command stored with a policy path from an OLD
    candidate slot (now deleted/replaced) must:
      1. Resolve to the CURRENT slot's policy via resolve_active_runtime_artifact
      2. Discover "nemotron" as an enabled provider in the current policy
      3. Probe nemotron → PASS
      4. Write retry_after_epoch=0 (wake signal)
      5. Emit provider.healthy_alternate_wake event
      6. On next _recover_one call, detect wake → RESOURCE_WAKE_RESUME → spawn worker
    Without ANY state loss, lineage mutation, or duplicate work.
    """
    # --- Setup: old slot (dead) and current slot (active) ---
    old_slot = tmp_path / "old_slot_deadbeef"
    old_descriptors = old_slot / "descriptors"
    old_descriptors.mkdir(parents=True)
    old_policy = old_descriptors / "nemotron.planner-policy.json"
    old_policy.write_text(json.dumps({
        "providers": {
            "deprecated_provider": {"enabled": True, "billing_class": "FREE"},
        },
    }), encoding="utf-8")

    current_slot = tmp_path / "current_slot_active"
    current_descriptors = current_slot / "descriptors"
    current_descriptors.mkdir(parents=True)
    current_policy = current_descriptors / "nemotron.planner-policy.json"
    current_policy.write_text(json.dumps({
        "providers": {
            "nemotron": {
                "enabled": True,
                "billing_class": "FREE",
                "model_id": "nvidia/nemotron-3-ultra-550b-a55b",
                "cloud_local": "CLOUD",
            },
            "groq": {
                "enabled": True,
                "billing_class": "FREE",
                "model_id": "openai/gpt-oss-120b",
                "cloud_local": "CLOUD",
            },
        },
    }), encoding="utf-8")

    cfg = _config(tmp_path, current_policy)
    cfg["runtime_slot_root"] = str(current_slot)
    monkeypatch.setenv("AOS_RUNTIME_SLOT_ROOT", str(current_slot))

    engine = RuntimeEngine(cfg)
    try:
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        # --- Create protected LARI lineage command with OLD slot policy path ---
        command_id = "continue-protected-lari-lineage"
        _command(engine, command_id, old_policy)

        # Seed the checkpoint so recovery sees WAITING_FOR_REASONING_PROVIDER
        project_runtime = engine.store.command_dir(command_id) / "project-runtime"
        project_runtime.mkdir(parents=True, exist_ok=True)
        checkpoint = {
            "phase": "WAITING_FOR_REASONING_PROVIDER",
            "batch_number": 498,
            "completed_batch_count": 498,
        }
        (project_runtime / "planning-kernel-checkpoint.json").write_text(
            json.dumps(checkpoint), encoding="utf-8"
        )

        # Pre-compute the recovery fingerprint so same_fingerprint_respawns
        # increments correctly (>= 3) → enters the wake detection path
        from aos.runtime_worker import build_recovery_fingerprint
        seed_state = {"completed_batch_count": 498}
        fingerprint = build_recovery_fingerprint(
            project_id="lari", state=seed_state, checkpoint=checkpoint,
        )

        # Put command into WAITING_FOR_REASONING_PROVIDER with resource hold
        engine.store.write_state(
            command_id,
            state="WAITING_FOR_REASONING_PROVIDER",
            disposition="WAITING_FOR_RESOURCE",
            failure_class="PROVIDER_RESOURCE_WAIT",
            recovery_disposition="WAITING_FOR_RESOURCE",
            recovery_fingerprint=fingerprint,
            recovery_fingerprint_sha256=fingerprint["fingerprint_sha256"],
            same_fingerprint_respawns=15,
            retry_after_epoch=time.time() + 600,  # far future
            completed_batch_count=498,
            completed_count_baseline=498,
            worker_pid=None,
        )

        # --- Verify the stored command still points to OLD policy (lineage preserved) ---
        durable_cmd = engine.store.read_command(command_id)
        assert durable_cmd["project"]["routing_policy_path"] == str(old_policy)

        # --- Stub provider probe to return Nemotron PASS ---
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda _path, providers: {
            provider_id: {
                "provider_id": provider_id,
                "credential_available": True,
                "local_service_available": None,
                "probe_attempted": True,
                "probe_status": "PASS",
                "failure_class": None,
                "last_observed_at": "2026-09-27T00:00:00+00:00",
                "latency_ms": 2500,
                "model_id": f"model-{provider_id}",
                "task_class": "small_reasoning",
            }
            for provider_id in providers
        })
        monkeypatch.setattr("aos.runtime_server.pid_alive", lambda _pid: False)

        # --- Step 3: Run probe cycle → should discover nemotron via CURRENT slot policy ---
        engine._run_live_probe_cycle()

        # --- Verify wake signal was written ---
        state_after_probe = engine.store.read_state(command_id)
        assert float(state_after_probe["retry_after_epoch"]) == 0.0, \
            f"Wake signal not written: retry_after_epoch={state_after_probe['retry_after_epoch']}"

        # Verify the wake event was emitted
        events = engine.store.read_events(command_id, after_seq=0)
        wake_events = [e for e in events if e.get("event_type") == "provider.healthy_alternate_wake"]
        assert len(wake_events) >= 1, f"No provider.healthy_alternate_wake event found"
        wake_payload = wake_events[-1]["payload"]
        assert wake_payload["lineage_preserved"] is True
        assert "nemotron" in wake_payload["healthy_providers"]

        # --- Step 4: Recovery detects wake → RESOURCE_WAKE_RESUME → spawn ---
        spawn_calls = []
        monkeypatch.setattr(
            engine, "_spawn_worker",
            lambda cid, recovered: spawn_calls.append((cid, recovered)) or 123,
        )

        engine._recover_one(command_id)

        state_resumed = engine.store.read_state(command_id)
        assert state_resumed["recovery_disposition"] == "RESOURCE_WAKE_RESUME", \
            f"Expected RESOURCE_WAKE_RESUME, got {state_resumed['recovery_disposition']}"
        assert state_resumed["same_fingerprint_respawns"] == 1, \
            "Churn counter should be reset to 1 after wake"
        assert len(spawn_calls) == 1, f"Expected 1 spawn, got {len(spawn_calls)}"
        assert spawn_calls[0] == (command_id, True)

        # --- Verify lineage integrity: command NOT rewritten ---
        durable_cmd_after = engine.store.read_command(command_id)
        assert durable_cmd_after["project"]["routing_policy_path"] == str(old_policy), \
            "Durable command lineage must NOT be rewritten by probe/wake cycle"

        # --- Verify completed_batch_count preserved (no state loss) ---
        assert int(state_resumed["completed_batch_count"]) == 498, \
            f"Batch count changed: expected 498, got {state_resumed['completed_batch_count']}"

        # --- Verify recovery event was emitted ---
        events_after = engine.store.read_events(command_id, after_seq=0)
        wake_resume_events = [e for e in events_after if e.get("event_type") == "runtime.resource_wake_resume"]
        assert len(wake_resume_events) >= 1, "No runtime.resource_wake_resume event found"
        resume_payload = wake_resume_events[-1]["payload"]
        assert resume_payload["reason"] == "PROVIDER_HEALTH_CONFIRMED_WAKE"

    finally:
        engine.shutdown()
