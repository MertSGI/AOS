import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aos.controller_relay import AsyncControllerRelay
from aos.process_utils import process_alive, run_headless
from aos.runtime_maintenance import PAUSED_SAFE, persist_maintenance, read_maintenance
from aos.runtime_server import RuntimeEngine


def _config(tmp_path: Path) -> dict:
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text('{"providers":{}}', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "contract_version": "1.0.0",
        "runtime_root": str(tmp_path / "state"),
        "authorized_roots": [str(tmp_path)],
        "default_project": "test",
        "projects": {
            "test": {
                "project_id": "test",
                "descriptor_path": str(descriptor),
                "routing_policy_path": str(policy),
                "workspace": str(workspace),
                "standing_authority": True,
            }
        },
        "production": "NO_GO",
        "ag_backend_enabled": False,
        "candidate_source_sha": "a" * 40,
        "runtime_slot_id": "candidate-test",
        "controller_relay_dir": str(tmp_path / "relay"),
    }


def test_persisted_pause_is_loaded_before_autonomous_threads(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    runtime_root = Path(config["runtime_root"])
    persist_maintenance(runtime_root, paused=True, reason="test_restart")
    probe_calls = []
    spawn_calls = []
    monkeypatch.setattr(RuntimeEngine, "_run_live_probe_cycle", lambda self: probe_calls.append(True))
    monkeypatch.setattr("aos.runtime_server.popen_headless", lambda *a, **k: spawn_calls.append((a, k)))
    engine = RuntimeEngine(config)
    try:
        assert engine.is_paused is True
        assert engine.health()["maintenance_state"] == PAUSED_SAFE
        time.sleep(0.1)
        assert probe_calls == []
        assert spawn_calls == []
    finally:
        engine.shutdown()


def test_pause_and_resume_are_atomic_and_restart_persistent(tmp_path: Path):
    config = _config(tmp_path)
    engine = RuntimeEngine(config)
    try:
        engine.pause_safe()
        assert read_maintenance(Path(config["runtime_root"]))["state"] == PAUSED_SAFE
    finally:
        engine.shutdown()
    restarted = RuntimeEngine(config)
    try:
        assert restarted.is_paused is True
        restarted.resume()
        assert read_maintenance(Path(config["runtime_root"]))["state"] == "RUNNING"
    finally:
        restarted.shutdown()


def test_health_is_constant_work_with_500_historical_commands(tmp_path: Path, monkeypatch):
    config = _config(tmp_path)
    runtime_root = Path(config["runtime_root"])
    persist_maintenance(runtime_root, paused=True, reason="health_test")
    commands = runtime_root / "commands"
    for index in range(550):
        command = commands / f"history-{index:04d}"
        command.mkdir(parents=True)
        (command / "state.json").write_text('{"state":"PROJECT_COMPLETE"}', encoding="utf-8")
    engine = RuntimeEngine(config)
    try:
        monkeypatch.setattr(engine.store, "list_command_ids", lambda: (_ for _ in ()).throw(AssertionError("history traversal")))
        monkeypatch.setattr(engine, "_provider_evidence", lambda: (_ for _ in ()).throw(AssertionError("provider traversal")))
        started = time.perf_counter()
        for _ in range(1000):
            health = engine.health()
        elapsed = time.perf_counter() - started
        assert health["runtime_state"] == "HEALTHY"
        assert elapsed < 0.25
    finally:
        engine.shutdown()


def test_async_relay_never_blocks_lifecycle_caller():
    class SlowPublisher:
        def emit_cycle(self, **kwargs):
            time.sleep(0.5)
            return type("Snapshot", (), {"sequence_number": 1})()

    relay = AsyncControllerRelay(SlowPublisher())
    try:
        started = time.perf_counter()
        assert relay.submit(runtime_health_dict={}) is True
        assert time.perf_counter() - started < 0.1
        assert relay.submit(maintenance=True, runtime_health_dict={}) is False
    finally:
        relay.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object acceptance")
def test_timeout_kills_child_and_descendant_tree(tmp_path: Path):
    child_pid = tmp_path / "child.pid"
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_headless([sys.executable, "-c", parent_code], timeout=1)
    deadline = time.monotonic() + 5
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid.exists()
    descendant = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while process_alive(descendant) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_alive(descendant) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows Git timeout acceptance")
def test_real_git_cli_timeout_is_bounded():
    command = [
        "git",
        "-c",
        'alias.aos-hang=!python -c "import time; time.sleep(60)"',
        "aos-hang",
    ]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_headless(command, timeout=1)
    assert time.monotonic() - started < 5


def test_production_process_contract_is_centralized_and_bounded():
    roots = [Path("src/aos"), Path("extensions")]
    raw_allowed = {Path("src/aos/process_utils.py")}
    violations = []
    for root in roots:
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "run_headless":
                    if not any(keyword.arg == "timeout" for keyword in node.keywords):
                        violations.append(f"{path}:{node.lineno}:run_headless_without_timeout")
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                    and path not in raw_allowed
                ):
                    violations.append(f"{path}:{node.lineno}:raw_subprocess_{node.func.attr}")
    assert violations == []
