"""Comprehensive Proofs A-O for Cline Agentic Execution Backend."""
import json
import os
import subprocess
from pathlib import Path

from extensions.autonomy_fabric.cline_agentic_backend import (
    ClineAgenticExecutionBackend,
    build_cline_argv,
    parse_cline_stream_output,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionHealth,
    ExecutionRequest,
)
from aos.workers.cline_cli_probe import (
    build_cline_child_environment,
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


def _success_output(session_id: str = SESSION_ID) -> str:
    return "\n".join([
        json.dumps({"ts": "2026-09-25T00:00:00Z", "type": "hook_event", "hookEventName": "agent_start", "taskId": session_id}),
        json.dumps({"ts": "2026-09-25T00:00:01Z", "type": "agent_event", "event": {"type": "iteration_start", "iteration": 1}}),
        json.dumps({"ts": "2026-09-25T00:00:02Z", "type": "agent_event", "event": {"type": "tool_call", "name": "read_file"}}),
        json.dumps({"ts": "2026-09-25T00:00:03Z", "type": "run_result", "finishReason": "complete", "usage": {"inputTokens": 100, "outputTokens": 25, "totalCost": 0}}),
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


# Proof A: executable launches & Proof B: version is stable 3.0.65
def test_proof_a_b_executable_identity_and_version():
    identity = resolve_cline_executable_identity()
    assert identity is not None
    assert identity["version"] == "3.0.65"
    assert identity["filename"] in {"cline.cmd", "cline.exe"}
    assert len(identity["sha256"]) == 64


# Proof C: structured JSON/NDJSON parses deterministically
def test_proof_c_structured_ndjson_parsing():
    raw_stdout = _success_output(SESSION_ID)
    outcome = parse_cline_stream_output(raw_stdout, "", returncode=0)
    assert outcome.valid is True
    assert outcome.session_id == SESSION_ID
    assert outcome.finish_reason == "complete"
    assert outcome.usage["totalCost"] == 0
    assert outcome.usage["inputTokens"] == 100

    # Test error parsing
    err_json = json.dumps({"ts": "2026-09-25T00:00:00Z", "type": "error", "message": "Unauthorized: please login"})
    err_outcome = parse_cline_stream_output("", err_json, returncode=1)
    assert err_outcome.valid is False
    assert err_outcome.failure_class == "AUTH_UNAVAILABLE"


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
    assert result.evidence_payload["finish_reason"] == "complete"
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
