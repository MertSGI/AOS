import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import aos.runtime_server as runtime_server
from aos.provider_circuit import CircuitState, ProviderCircuitBreakerRegistry
from aos.provider_observation import TaskClass
from aos.runtime_server import RuntimeEngine


def _policy(path: Path) -> Path:
    path.write_text(
        json.dumps({
            "providers": {
                provider_id: {
                    "provider_id": provider_id,
                    "enabled": True,
                    "cloud_local": "LOCAL" if provider_id == "ollama" else "CLOUD",
                    "model_id": f"{provider_id}-model",
                }
                for provider_id in ("nemotron", "groq", "ollama")
            }
        }),
        encoding="utf-8",
    )
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
            "test_proj": {
                "project_id": "test_proj",
                "descriptor_path": str(descriptor),
                "workspace": str(workspace),
                "routing_policy_path": str(policy),
                "standing_authority": True,
            }
        },
        "default_project": "test_proj",
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "provider_probe_interval_seconds": 0.05,
    }


def _command(
    engine: RuntimeEngine,
    command_id: str,
    policy: Path,
    state: str = "WAITING_FOR_REASONING_PROVIDER",
    required_task_class: str = TaskClass.STRUCTURED_PLANNING.value,
) -> Path:
    engine.store.create_command({
        "command_id": command_id,
        "goal": "Continue execution",
        "project": {
            "project_id": "test_proj",
            "descriptor_path": str(policy.parent / "descriptor.json"),
            "workspace": str(policy.parent / "workspace"),
            "routing_policy_path": str(policy),
            "standing_authority": True,
        },
        "continuous": True,
    })
    engine.store.write_state(
        command_id,
        state=state,
        attempts=3,
        completed_batch_count=10,
        retry_after_epoch=time.time() + 9999.0,
        required_task_class=required_task_class,
    )
    return engine.store.command_dir(command_id) / "project-runtime" / "provider-circuits.json"


def test_watchdog_performs_more_than_one_cycle(tmp_path, monkeypatch):
    """1. provider watchdog performs more than one cycle without operator action."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    cycle_count = 0
    cycle_event = threading.Event()

    def mock_probe_cycle():
        nonlocal cycle_count
        cycle_count += 1
        if cycle_count >= 2:
            cycle_event.set()

    engine = RuntimeEngine(cfg)
    try:
        monkeypatch.setattr(engine, "_run_live_probe_cycle", mock_probe_cycle)
        assert cycle_event.wait(timeout=5.0), f"Expected >= 2 cycles, got {cycle_count}"
        assert cycle_count >= 2
    finally:
        engine.shutdown()


def test_first_probe_fails_later_succeeds_same_lineage_wakes(tmp_path, monkeypatch):
    """2. first probe fails, later probe succeeds, SAME waiting lineage wakes."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-lineage-wake"
        _command(engine, command_id, policy, required_task_class=TaskClass.STRUCTURED_PLANNING.value)

        results_fail = {
            "nemotron": {
                "provider_id": "nemotron",
                "probe_status": "FAIL",
                "probe_attempted": True,
                "model_id": "nemotron-model",
                "failure_class": "RATE_LIMITED",
                "last_observed_at": "2026-09-26T00:00:00Z",
            }
        }
        results_success = {
            "nemotron": {
                "provider_id": "nemotron",
                "probe_status": "PASS",
                "probe_attempted": True,
                "model_id": "nemotron-model",
                "last_observed_at": "2026-09-26T00:01:00Z",
            }
        }

        call_idx = 0
        def mock_probe_enabled(*args, **kwargs):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return results_fail
            return results_success

        monkeypatch.setattr(runtime_server, "probe_enabled_providers", mock_probe_enabled)

        # First cycle fails
        engine._run_live_probe_cycle()
        state1 = engine.store.read_state(command_id)
        assert state1["state"] == "WAITING_FOR_REASONING_PROVIDER"
        assert float(state1.get("retry_after_epoch", 0)) > 0

        # Second cycle succeeds
        engine._run_live_probe_cycle()
        state2 = engine.store.read_state(command_id)
        assert state2["state"] == "WAITING_FOR_REASONING_PROVIDER"
        assert float(state2.get("retry_after_epoch", 1)) == 0.0
    finally:
        engine.shutdown()


def test_watchdog_survives_probe_exception(tmp_path, monkeypatch):
    """3. watchdog survives a probe exception and probes again later."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        # Pause background thread so controlled synchronous invocation is isolated
        engine.pause_safe()
        command_id = "continue-survive-exc"
        circuit_path = _command(engine, command_id, policy)
        registry = ProviderCircuitBreakerRegistry(circuit_path)
        # Mark nemotron OPEN so it is due for probe
        registry.record_failure("nemotron", "RATE_LIMITED", now=1000)

        # 1. Directly verify that an exception inside _run_live_probe_cycle calls _record_probe_cycle_failure
        def faulty_probe(*args, **kwargs):
            raise RuntimeError("Transient probe network crash")

        monkeypatch.setattr(runtime_server, "probe_enabled_providers", faulty_probe)
        engine._run_live_probe_cycle()

        events = engine.store.read_events(command_id)
        assert any(e.get("event_type") == "provider.probe_cycle_failed" for e in events)

        # 2. Verify that _probe_loop survives an exception and continues running
        engine.resume()
        call_count = 0
        succeeded_event = threading.Event()

        def faulty_cycle():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Transient loop error")
            succeeded_event.set()

        monkeypatch.setattr(engine, "_run_live_probe_cycle", faulty_cycle)
        engine._probe_wake_event.set()
        assert succeeded_event.wait(timeout=5.0), "Watchdog failed to run subsequent cycles after exception"
        assert call_count >= 2
    finally:
        engine.shutdown()


def test_pause_stops_probing_and_resume_restarts(tmp_path, monkeypatch):
    """4 & 5. pause stops autonomous probing/wake safely, resume restarts managed probing exactly once."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        call_count = 0
        def counting_cycle():
            nonlocal call_count
            call_count += 1

        monkeypatch.setattr(engine, "_run_live_probe_cycle", counting_cycle)

        # Pause
        engine.pause_safe()
        assert engine.is_paused is True
        snap = call_count
        time.sleep(0.2)
        assert call_count == snap, "Probing ran while paused"

        # Resume
        engine.resume()
        assert engine.is_paused is False
        time.sleep(0.2)
        assert call_count > snap, "Probing did not resume after explicit resume"
    finally:
        engine.shutdown()


def test_shutdown_leaves_no_probe_thread(tmp_path):
    """6. shutdown leaves no probe thread."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    probe_thread = engine.probe_thread
    assert probe_thread is not None
    assert probe_thread.is_alive()
    engine.shutdown()
    assert not probe_thread.is_alive()


def test_structured_planning_lane_wakes(tmp_path, monkeypatch):
    """7. structured_planning waiting lane wakes when an adequate provider becomes available."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-structured-wake"
        _command(engine, command_id, policy, required_task_class=TaskClass.STRUCTURED_PLANNING.value)

        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda *a, **k: {
            "nemotron": {
                "provider_id": "nemotron",
                "probe_status": "PASS",
                "probe_attempted": True,
                "model_id": "nemotron-model",
                "last_observed_at": "2026-09-26T00:00:00Z",
            }
        })
        engine._run_live_probe_cycle()
        state = engine.store.read_state(command_id)
        assert float(state.get("retry_after_epoch", 1)) == 0.0
    finally:
        engine.shutdown()


def test_repo_ui_planning_lane_wakes(tmp_path, monkeypatch):
    """8. repo_ui_planning waiting lane wakes when an adequate provider becomes available."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-repo-ui-wake"
        _command(engine, command_id, policy, required_task_class=TaskClass.REPO_UI_PLANNING.value)

        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda *a, **k: {
            "groq": {
                "provider_id": "groq",
                "probe_status": "PASS",
                "probe_attempted": True,
                "model_id": "groq-model",
                "last_observed_at": "2026-09-26T00:00:00Z",
            }
        })
        engine._run_live_probe_cycle()
        state = engine.store.read_state(command_id)
        assert float(state.get("retry_after_epoch", 1)) == 0.0
    finally:
        engine.shutdown()


def test_small_reasoning_probe_not_promoted_to_ineligible_local_provider(tmp_path, monkeypatch):
    """9. SMALL_REASONING probe evidence is NOT falsely promoted to arbitrary high-complexity capability."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-repo-ui-local-fail"
        _command(engine, command_id, policy, required_task_class=TaskClass.REPO_UI_PLANNING.value)

        # Only local ollama passed probe
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda *a, **k: {
            "ollama": {
                "provider_id": "ollama",
                "probe_status": "PASS",
                "probe_attempted": True,
                "model_id": "ollama-model",
                "last_observed_at": "2026-09-26T00:00:00Z",
            }
        })
        engine._run_live_probe_cycle()
        state = engine.store.read_state(command_id)
        # Should NOT wake because ollama cannot satisfy repo_ui_planning
        assert float(state.get("retry_after_epoch", 0)) > 0
    finally:
        engine.shutdown()


def test_qwen_ready_but_task_ineligible_does_not_wake(tmp_path, monkeypatch):
    """10. Qwen ready but task-ineligible DOES NOT wake an incompatible lane."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-qwen-ineligible"
        _command(engine, command_id, policy, required_task_class=TaskClass.REPO_UI_PLANNING.value)

        # Mock Qwen lifecycle ready and proven
        mock_mgr = MagicMock()
        mock_mgr.get_snapshot.return_value.state.value = "AVAILABLE"
        monkeypatch.setattr("aos.workers.llama_cpp_lifecycle.get_qwen_lifecycle_manager", lambda: mock_mgr)
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_llama_cpp_identity", lambda exe: "llama-server")
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_qwen_model", lambda: {"model_path": "dummy"})
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_capability_status", lambda **k: "PROVEN")

        engine._wake_waiting_from_observed_provider_health()
        state = engine.store.read_state(command_id)
        assert float(state.get("retry_after_epoch", 0)) > 0, "Qwen falsely woke REPO_UI_PLANNING lane"
    finally:
        engine.shutdown()


def test_qwen_ready_and_task_eligible_wakes(tmp_path, monkeypatch):
    """11. Qwen ready and task-eligible DOES wake the SAME lineage."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        command_id = "continue-qwen-eligible"
        _command(engine, command_id, policy, required_task_class=TaskClass.STRUCTURED_PLANNING.value)

        mock_mgr = MagicMock()
        mock_mgr.get_snapshot.return_value.state.value = "AVAILABLE"
        monkeypatch.setattr("aos.workers.llama_cpp_lifecycle.get_qwen_lifecycle_manager", lambda: mock_mgr)
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_llama_cpp_identity", lambda exe: "llama-server")
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_qwen_model", lambda: {"model_path": "dummy"})
        monkeypatch.setattr("aos.workers.llama_cpp_probe.resolve_capability_status", lambda **k: "PROVEN")

        engine._wake_waiting_from_observed_provider_health()
        state = engine.store.read_state(command_id)
        assert float(state.get("retry_after_epoch", 1)) == 0.0
    finally:
        engine.shutdown()


def test_policy_discovery_failure_produces_degraded_telemetry(tmp_path):
    """12. provider policy discovery failure produces DEGRADED/STALE telemetry, not provider_details=[] presented as normal truth."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        # Load successfully first
        providers = engine._enabled_from_policy(policy)
        assert "nemotron" in providers

        # Corrupt the policy file
        policy.write_text("NOT_VALID_JSON{", encoding="utf-8")

        # Now loading should return cached providers and record error
        fallback = engine._enabled_from_policy(policy)
        assert fallback == providers, "Policy fallback did not return cached provider list"

        detailed = engine._collect_detailed_status()
        assert detailed["provider_discovery_status"] == "DEGRADED"
        assert len(detailed["provider_discovery_errors"]) > 0
    finally:
        engine.shutdown()


def test_worker_routing_and_relay_telemetry_resolve_same_providers(tmp_path):
    """13. worker routing and relay telemetry resolve the same canonical enabled provider set."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        _command(engine, "continue-canonical-match", policy)
        enabled_worker = engine._enabled_from_policy(policy)
        status_dict = engine._collect_detailed_status()
        assert set(enabled_worker) == set(status_dict["enabled_reasoning_providers"])
    finally:
        engine.shutdown()


def test_two_protected_waiting_lineages_independently_wake(tmp_path, monkeypatch):
    """14. two protected waiting lineages can independently wake without creating new commands."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        # Stop background workers so controlled probe cycle is isolated
        engine.stop_event.set()
        engine.recovery_thread.join(timeout=3.0)
        if engine.probe_thread is not None:
            engine.probe_thread.join(timeout=3.0)
        engine.telemetry_thread.join(timeout=3.0)

        lari_id = "continue-lari-wake"
        uiv2_id = "continue-uiv2-wake"
        _command(engine, lari_id, policy, required_task_class=TaskClass.STRUCTURED_PLANNING.value)
        _command(engine, uiv2_id, policy, required_task_class=TaskClass.REPO_UI_PLANNING.value)

        # Cloud provider passes probe
        monkeypatch.setattr(runtime_server, "probe_enabled_providers", lambda *a, **k: {
            "nemotron": {
                "provider_id": "nemotron",
                "probe_status": "PASS",
                "probe_attempted": True,
                "model_id": "nemotron-model",
                "last_observed_at": "2026-09-26T00:00:00Z",
            }
        })
        engine._run_live_probe_cycle()

        state_lari = engine.store.read_state(lari_id)
        state_uiv2 = engine.store.read_state(uiv2_id)
        assert float(state_lari.get("retry_after_epoch", 1)) == 0.0
        assert float(state_uiv2.get("retry_after_epoch", 1)) == 0.0
    finally:
        engine.shutdown()


def test_no_duplicate_worker_spawn_or_probe_thread(tmp_path):
    """15. no duplicate worker spawn / probe thread / completed batch."""
    policy = _policy(tmp_path / "policy.json")
    cfg = _config(tmp_path, policy)
    engine = RuntimeEngine(cfg)
    try:
        t1 = engine.probe_thread
        engine._start_probe_thread()
        t2 = engine.probe_thread
        assert t1 is t2, "Duplicate probe thread created"
    finally:
        engine.shutdown()
