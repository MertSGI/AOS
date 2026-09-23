import json
import subprocess
from pathlib import Path

from extensions.autonomy_fabric.codex_cli_backend import (
    CodexCliExecutionBackend,
    build_codex_exec_argv,
    parse_codex_exec_jsonl,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)


THREAD_ID = "12345678-1234-4234-8234-123456789abc"
IDENTITY = {
    "path": "codex-test-double",
    "filename": "codex-test-double",
    "sha256": "c" * 64,
    "version": "codex-cli test",
}


def _repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "codex@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=tmp_path, check=True)
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
        project_id="project-1",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        write_scope=["allowed"],
        payload={
            "prompt": "Perform bounded work",
            "source_sha": source_sha,
            "checkpoint_id": "checkpoint-1",
            "objective_id": "objective-1",
        },
    )


def _success_output(thread_id: str = THREAD_ID) -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "secret output"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
    ])


def _backend(runner, *, capability="TEST_DOUBLE", quota=None):
    return CodexCliExecutionBackend(
        runner=runner,
        capability_status_provider=lambda: capability,
        quota_snapshot_provider=lambda: quota or {"state": "AVAILABLE", "source": "TEST"},
        executable_identity=IDENTITY,
    )


def test_argv_places_safety_flags_before_exec_and_uses_stdin_marker(tmp_path):
    argv = build_codex_exec_argv("codex", str(tmp_path))
    assert argv == [
        "codex", "--ask-for-approval", "never", "--sandbox", "workspace-write",
        "--cd", str(tmp_path), "exec", "--ignore-user-config", "--json", "-",
    ]
    resumed = build_codex_exec_argv("codex", str(tmp_path), thread_id=THREAD_ID)
    assert resumed[-3:] == ["resume", THREAD_ID, "-"]
    assert "--last" not in resumed
    assert not any("dangerously-bypass" in value for value in resumed)


def test_start_and_exact_resume_use_stdin_and_scrub_api_credentials(tmp_path, monkeypatch):
    source_sha = _repo(tmp_path)
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-pass")

    def runner(argv, cwd, prompt, timeout, env):
        calls.append((argv, cwd, prompt, timeout, env))
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    backend = _backend(runner)
    first = backend.execute(_request(tmp_path, source_sha))
    assert first.status == "SUCCESS"
    assert first.agentic_identity.session_or_thread_id == THREAD_ID
    assert calls[0][2] == "Perform bounded work"
    assert "OPENAI_API_KEY" not in calls[0][4]
    assert "CODEX_API_KEY" not in calls[0][4]

    request = _request(tmp_path, source_sha, task_id="work-2")
    request.agentic_identity = first.agentic_identity
    resumed = backend.execute(request)
    assert resumed.status == "SUCCESS"
    assert calls[1][0][-3:] == ["resume", THREAD_ID, "-"]
    assert resumed.agentic_identity.last_successful_turn == 2
    serialized = json.dumps(resumed.to_dict())
    assert "secret output" not in serialized
    assert "Perform bounded work" not in serialized


def test_stale_or_completed_resume_never_invokes_cli(tmp_path):
    source_sha = _repo(tmp_path)
    calls = []

    def runner(argv, cwd, prompt, timeout, env):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    backend = _backend(runner)
    first = backend.execute(_request(tmp_path, source_sha))
    duplicate = _request(tmp_path, source_sha)
    duplicate.agentic_identity = first.agentic_identity
    duplicated = backend.execute(duplicate)
    assert duplicated.sanitized_errors == ["COMPLETED_WORK_MUST_NOT_BE_DUPLICATED"]

    (tmp_path / "base.txt").write_text("changed\n", encoding="utf-8")
    stale = _request(tmp_path, source_sha, task_id="work-2")
    stale.agentic_identity = first.agentic_identity
    rejected = backend.execute(stale)
    assert rejected.sanitized_errors == ["STALE_AGENT_SESSION:WORKSPACE_CHANGED"]
    assert len(calls) == 1


def test_quota_or_unproven_auth_is_nonterminal_and_never_invoked(tmp_path):
    source_sha = _repo(tmp_path)
    calls = []
    runner = lambda *args: calls.append(args)
    exhausted = _backend(runner, quota={"state": "QUOTA_EXHAUSTED", "retry_after_epoch": 42})
    result = exhausted.execute(_request(tmp_path, source_sha))
    assert result.status == "DEGRADED"
    assert result.availability.state.value == "QUOTA_EXHAUSTED"
    assert calls == []

    unproven = _backend(runner, capability="UNPROVEN")
    denied = unproven.execute(_request(tmp_path, source_sha))
    assert denied.availability.state.value == "AUTH_UNAVAILABLE"
    assert calls == []


def test_parser_requires_one_thread_and_one_success_terminal():
    assert parse_codex_exec_jsonl(_success_output(), returncode=0).valid
    cases = [
        ("not json", 0, "CONTRACT_FAILURE"),
        (json.dumps({"type": "unknown.event"}), 0, "CONTRACT_FAILURE"),
        (json.dumps({"type": "item.unknown"}), 0, "CONTRACT_FAILURE"),
        (json.dumps({"type": "thread.started", "thread_id": THREAD_ID}), 0, "CONTRACT_FAILURE"),
        (_success_output(), 1, "CONTRACT_FAILURE"),
        ("\n".join([
            json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
            json.dumps({"type": "turn.failed", "error": {"code": "rate_limit"}}),
        ]), 1, "QUOTA_EXHAUSTED"),
    ]
    for output, code, failure in cases:
        outcome = parse_codex_exec_jsonl(output, returncode=code)
        assert not outcome.valid
        assert outcome.failure_class == failure


def test_write_scope_escape_fails_closed(tmp_path):
    source_sha = _repo(tmp_path)

    def runner(argv, cwd, prompt, timeout, env):
        (tmp_path / "outside.txt").write_text("escape\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _success_output(), "")

    result = _backend(runner).execute(_request(tmp_path, source_sha))
    assert result.status == "FAILED"
    assert result.sanitized_errors == ["CODEX_WRITE_SCOPE_VIOLATION"]


def test_host_registers_codex_outside_provider_factories(tmp_path):
    from aos.autonomous_host import _PROVIDER_FACTORIES, build_execution_router

    policy = Path(__file__).resolve().parents[3] / "descriptors" / "nemotron.planner-policy.json"
    router = build_execution_router(policy, tmp_path / "runtime")
    backend = router.get_backend("codex_cli")
    assert isinstance(backend, CodexCliExecutionBackend)
    assert backend.cost.value == "SUBSCRIPTION_INCLUDED"
    assert "codex_cli" not in _PROVIDER_FACTORIES
