"""Comprehensive Proofs A-O for Cline Agentic Execution Backend."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from extensions.autonomy_fabric.cline_agentic_backend import (
    ClineAgenticExecutionBackend,
    build_cline_argv,
    parse_cline_stream_output,
)
from extensions.autonomy_fabric.execution_backend import (
    AgenticSessionIdentity,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionRequest,
    ExecutionAvailabilityState,
)
from aos.workers.cline_cli_probe import (
    build_cline_child_environment,
    get_cline_runtime_config_path,
    load_cline_runtime_config,
    resolve_cline_capability_status,
    resolve_cline_executable_identity,
)

SESSION_ID = "conv_1790318420567_3ui6zat"
IDENTITY = {
    "path": "cline-cmd-double",
    "filename": "cline.cmd",
    "sha256": "f" * 64,
    "version": "3.0.65",
}


def _repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "cline@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Cline Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _request(tmp_path: Path, source_sha: str, *, task_id: str = "work-1") -> ExecutionRequest:
    return ExecutionRequest(
        task_id=task_id,
        project_id="project-cline",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-CLINE-1",
        write_scope=["allowed"],
        payload={
            "prompt": "Perform bounded work",
            "source_sha": source_sha,
            "checkpoint_id": "checkpoint-1",
            "objective_id": "objective-1",
        },
    )


def _success_output(session_id: str = SESSION_ID, *, text: str | None = None) -> str:
    return "\n".join([
        json.dumps({"ts": "2026-09-25T00:00:00Z", "type": "hook_event", "hookEventName": "agent_start", "taskId": session_id}),
        json.dumps({"ts": "2026-09-25T00:00:01Z", "type": "agent_event", "event": {"type": "iteration_start", "iteration": 1}}),
        json.dumps({"ts": "2026-09-25T00:00:02Z", "type": "agent_event", "event": {"type": "tool_call", "name": "read_file"}}),
        json.dumps({"ts": "2026-09-25T00:00:03Z", "type": "run_result", "finishReason": "completed", "usage": {"inputTokens": 100, "outputTokens": 25, "totalCost": 0}, "text": text}),
    ])


def _backend(runner, *, capability="TEST_DOUBLE", data_dir=None, config_dir=None):
    return ClineAgenticExecutionBackend(
        runner=runner,
        capability_status_provider=lambda: capability,
        executable_identity=IDENTITY,
        data_dir=data_dir,
        config_dir=config_dir,
        underlying_provider="openai-compatible",
        underlying_model="qwen-local",
        underlying_cost_class="FREE_LOCAL",
    )


# Proof A: executable launches & Proof B: accepted machine version is stable.
def test_proof_a_b_executable_identity_and_version():
    identity = resolve_cline_executable_identity()
    assert identity is not None
    assert identity["version"] == "3.0.68"
    assert identity["filename"] in {"cline.cmd", "cline.exe"}
    assert len(identity["sha256"]) == 64


# Proof C: structured JSON/NDJSON parses deterministically
def test_proof_c_structured_ndjson_parsing():
    raw_stdout = _success_output(SESSION_ID, text='{"answer":"bounded"}')
    outcome = parse_cline_stream_output(raw_stdout, "", returncode=0)
    assert outcome.valid is True
    assert outcome.session_id == SESSION_ID
    assert outcome.finish_reason == "completed"
    assert outcome.result_text == '{"answer":"bounded"}'
    assert outcome.usage["totalCost"] == 0
    assert outcome.usage["inputTokens"] == 100

    # Test error parsing
    err_json = json.dumps({"ts": "2026-09-25T00:00:00Z", "type": "error", "message": "Unauthorized: please login"})
    err_outcome = parse_cline_stream_output("", err_json, returncode=1)
    assert err_outcome.valid is False
    assert err_outcome.failure_class == "AUTH_UNAVAILABLE"

    overloaded = parse_cline_stream_output(
        "",
        json.dumps({"type": "error", "message": "Service temporarily overloaded"}),
        returncode=1,
    )
    assert overloaded.failure_class == "PROVIDER_CAPACITY"


def test_bounded_execution_timeout_is_resource_unavailable_not_contract_failure(tmp_path):
    source_sha = _repo(tmp_path)

    def runner(argv, cwd, prompt, timeout, env):
        return subprocess.CompletedProcess(argv, 124, "", "")

    backend = _backend(
        runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config")
    )
    result = backend.execute(_request(tmp_path, source_sha))

    assert result.status == "DEGRADED"
    assert result.sanitized_errors == ["CLINE_EXECUTION_TIMEOUT"]
    assert result.availability.state == ExecutionAvailabilityState.TEMPORARILY_UNAVAILABLE


# Proof D, E, F, G, H, I: execution, containment, safe write, process exec, terminal classification, session identity
def test_proof_d_e_f_g_h_i_execution_lifecycle(tmp_path):
    source_sha = _repo(tmp_path)
    calls = []

    def runner(argv, cwd, prompt, timeout, env):
        calls.append((argv, cwd, prompt, timeout, env))
        # simulate safe file write inside allowed write_scope
        allowed_dir = tmp_path / "allowed"
        allowed_dir.mkdir(exist_ok=True)
        (allowed_dir / "result.txt").write_text("computation complete\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    backend = _backend(runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    req = _request(tmp_path, source_sha)
    result = backend.execute(req)

    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert result.agentic_identity is not None
    assert result.agentic_identity.session_or_thread_id == SESSION_ID
    assert "allowed/result.txt" in result.changed_paths
    assert result.evidence_payload["finish_reason"] == "completed"
    assert len(calls) == 1
    argv = calls[0][0]
    assert "--json" in argv
    assert "--auto-approve" in argv
    assert "--data-dir" in argv
    assert "--config" in argv


# Proof J, K: session resume using exact session ID, retains context
def test_proof_j_k_session_resume(tmp_path):
    source_sha = _repo(tmp_path)
    calls = []

    def runner(argv, cwd, prompt, timeout, env):
        calls.append((argv, cwd, prompt, timeout, env))
        return subprocess.CompletedProcess(argv, 0, _success_output(SESSION_ID), "")

    backend = _backend(runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    req1 = _request(tmp_path, source_sha, task_id="task-1")
    res1 = backend.execute(req1)
    assert res1.status == "SUCCESS"

    req2 = _request(tmp_path, source_sha, task_id="task-2")
    req2.agentic_identity = res1.agentic_identity
    res2 = backend.execute(req2)
    assert res2.status == "SUCCESS"
    assert res2.agentic_identity.last_successful_turn == 2

    # Check argv of second call has --id SESSION_ID
    argv2 = calls[1][0]
    idx = argv2.index("--id")
    assert argv2[idx + 1] == SESSION_ID


# Proof L: changed workspace is detected before resume and rejected
def test_proof_l_workspace_stale_resume_rejected(tmp_path):
    source_sha = _repo(tmp_path)
    calls = []

    def runner(argv, cwd, prompt, timeout, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _success_output(SESSION_ID), "")

    backend = _backend(runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    req1 = _request(tmp_path, source_sha, task_id="task-1")
    res1 = backend.execute(req1)

    # Mutate workspace behind backend's back
    (tmp_path / "base.txt").write_text("externally modified\n", encoding="utf-8")

    req2 = _request(tmp_path, source_sha, task_id="task-2")
    req2.agentic_identity = res1.agentic_identity
    res2 = backend.execute(req2)
    assert res2.status == "DEGRADED"
    assert res2.sanitized_errors == ["STALE_AGENT_SESSION:WORKSPACE_CHANGED"]
    assert len(calls) == 1  # Never invoked CLI on stale workspace


# Proof M: no mutation outside allowed workspace (write scope containment)
def test_proof_m_containment_violation_fails(tmp_path):
    source_sha = _repo(tmp_path)

    def runner(argv, cwd, prompt, timeout, env):
        # mutate outside write_scope ("allowed")
        (tmp_path / "forbidden.txt").write_text("escape\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    backend = _backend(runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    req = _request(tmp_path, source_sha)
    result = backend.execute(req)
    assert result.status == "FAILED"
    assert "CLINE_WRITE_SCOPE_VIOLATION" in result.sanitized_errors


# Proof N: no secret persistence in AOS child environment or artifacts
def test_proof_n_secret_scrubbing(tmp_path, monkeypatch):
    source_sha = _repo(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("CLINE_API_KEY", "cline-secret")

    env = build_cline_child_environment()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLINE_API_KEY" not in env

    calls = []
    def runner(argv, cwd, prompt, timeout, passed_env):
        calls.append(passed_env)
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    backend = _backend(runner, data_dir=str(tmp_path / "data"), config_dir=str(tmp_path / "config"))
    res = backend.execute(_request(tmp_path, source_sha))
    assert res.status == "SUCCESS"
    assert "OPENAI_API_KEY" not in calls[0]


# Proof O: no paid calls (paid provider fallback disabled)
def test_proof_o_zero_paid_calls_contract():
    backend = _backend(lambda *args: None)
    availability = backend.get_availability()
    assert availability.evidence["paid_fallback"] == "DISABLED"
    assert availability.evidence["cost_class"] == "FREE_LOCAL"


def test_prelaunch_failure_identifies_source_sha_resolution_without_raw_error(tmp_path):
    backend = _backend(
        lambda *_args: pytest.fail("runner must not launch"),
        data_dir=str(tmp_path / "data"),
        config_dir=str(tmp_path / "config"),
    )
    result = backend.execute(_request(tmp_path, "not-a-sha"))

    assert result.evidence_payload == {
        "failure_class": "CLINE_PRELAUNCH_CONTRACT_FAILURE",
        "prelaunch_operation": "SOURCE_SHA_RESOLUTION",
        "exception_class": "ValueError",
    }


def test_prelaunch_failure_identifies_workspace_fingerprint_without_raw_error(tmp_path, monkeypatch):
    backend = _backend(
        lambda *_args: pytest.fail("runner must not launch"),
        data_dir=str(tmp_path / "data"),
        config_dir=str(tmp_path / "config"),
    )

    result = backend.execute(_request(tmp_path, "a" * 40))

    assert result.evidence_payload == {
        "failure_class": "CLINE_PRELAUNCH_CONTRACT_FAILURE",
        "prelaunch_operation": "WORKSPACE_FINGERPRINT",
        "exception_class": "WorkspaceFingerprintError",
        "fingerprint_operation": "GIT_RESOLVE_TOPLEVEL",
        "safe_cause": "GIT_EXIT_128",
    }
    assert str(tmp_path) not in json.dumps(result.to_dict())


def test_prelaunch_failure_identifies_prompt_construction_without_raw_error(tmp_path):
    source_sha = _repo(tmp_path)
    backend = _backend(
        lambda *_args: pytest.fail("runner must not launch"),
        data_dir=str(tmp_path / "data"),
        config_dir=str(tmp_path / "config"),
    )
    request = _request(tmp_path, source_sha)
    request.payload["prompt"] = "x" * 64_001

    result = backend.execute(request)

    assert result.evidence_payload == {
        "failure_class": "CLINE_PRELAUNCH_CONTRACT_FAILURE",
        "prelaunch_operation": "PROMPT_CONSTRUCTION",
        "exception_class": "ValueError",
    }


def test_machine_local_runtime_config_rejects_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = get_cline_runtime_config_path()
    path.parent.mkdir(parents=True)
    base = {
        "provider": "openai-compatible",
        "model": "nvidia/nemotron-3-ultra-550b-a55b",
        "config_dir": str((tmp_path / "cline-config").resolve()),
        "data_dir": str((tmp_path / "cline-data").resolve()),
        "cost_class": "FREE_TIER_CLOUD",
        "paid_fallback": "DISABLED",
    }
    path.write_text(json.dumps(base), encoding="utf-8")
    loaded = load_cline_runtime_config()
    assert loaded is not None
    assert loaded.provider == "openai-compatible"
    assert loaded.model == "nvidia/nemotron-3-ultra-550b-a55b"

    path.write_text(json.dumps({**base, "api_key": "must-not-be-here"}), encoding="utf-8")
    assert load_cline_runtime_config() is None


def test_real_mode_uses_durable_history_session_id_not_stream_task_id(tmp_path):
    source_sha = _repo(tmp_path)
    data_dir = tmp_path.parent / f"{tmp_path.name}-cline-data"
    durable_id = "1791344906919_y44r4"

    def runner(argv, cwd, prompt, timeout, env):
        session_dir = data_dir / "sessions" / durable_id
        session_dir.mkdir(parents=True)
        (session_dir / f"{durable_id}.json").write_text(json.dumps({
            "session_id": durable_id,
            "cwd": str(tmp_path.resolve()),
            "provider": "openai-compatible",
            "model": "qwen-local",
            "status": "completed",
        }), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _success_output("conv_stream_task"), "")

    backend = _backend(
        runner,
        capability="OPERATIONAL",
        data_dir=str(data_dir),
        config_dir=str(tmp_path / "cline-config"),
    )
    result = backend.execute(_request(tmp_path, source_sha))

    assert result.status == "SUCCESS"
    assert result.agentic_identity.session_or_thread_id == durable_id
    assert result.evidence_payload["stream_task_id"] == "conv_stream_task"
    assert result.evidence_payload["durable_session_id"] == durable_id


def test_start_only_attestation_fails_closed_for_resume(tmp_path):
    source_sha = _repo(tmp_path)
    backend = _backend(
        lambda *_args: pytest.fail("runner must not launch"),
        capability="OPERATIONAL_START_ONLY",
        data_dir=str(tmp_path / "cline-data"),
        config_dir=str(tmp_path / "cline-config"),
    )
    request = _request(tmp_path, source_sha, task_id="resume-work")
    identity = AgenticSessionIdentity(
        resource_id="cline_harness",
        backend_id="cline",
        session_or_thread_id="durable-session",
        workspace_fingerprint="a" * 64,
        source_sha=source_sha,
        checkpoint_id="checkpoint",
        last_successful_turn=1,
    )

    result = backend.resume(request, identity, {})

    assert result.status == "DEGRADED"
    assert result.sanitized_errors == ["CLINE_SESSION_RESUME_UNPROVEN"]
    assert result.evidence_payload["resume_mode"] == "FAIL_CLOSED"
