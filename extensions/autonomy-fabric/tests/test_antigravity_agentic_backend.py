import json
import subprocess
from pathlib import Path

from extensions.autonomy_fabric.antigravity_adapter import (
    AntigravityResponse,
    AntigravityStatus,
    FakeAntigravityAdapter,
)
from extensions.autonomy_fabric.antigravity_agentic_backend import (
    AntigravityAgenticExecutionBackend,
)
from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionRequest,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.supervisor import ParallelSupervisor


IDENTITY = {
    "path": "agy-test-double",
    "filename": "agy-test-double",
    "sha256": "a" * 64,
    "version": "agy-test-1.0",
}


def _repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ag@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "AG Test"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _request(tmp_path, source_sha, **kwargs):
    return ExecutionRequest(
        task_id=kwargs.pop("task_id", "work-1"),
        project_id="project-1",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="AUTH-1",
        payload={
            "prompt": "Perform bounded work",
            "source_sha": source_sha,
            "checkpoint_id": "checkpoint-1",
            "objective_id": "objective-1",
        },
        **kwargs,
    )


def _backend(adapter):
    return AntigravityAgenticExecutionBackend(
        adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity=IDENTITY,
    )


def test_start_and_exact_session_resume_are_fingerprint_gated(tmp_path):
    source_sha = _repo(tmp_path)
    adapter = FakeAntigravityAdapter()
    backend = _backend(adapter)

    first = backend.execute(_request(tmp_path, source_sha))
    assert first.status == "SUCCESS"
    assert first.agentic_identity is not None
    assert first.agentic_identity.session_or_thread_id == "conv-fake-1"
    assert first.agentic_identity.completed_work_unit_ids == ["work-1"]
    assert "Fake response" not in json.dumps(first.to_dict())

    resumed_request = _request(tmp_path, source_sha, task_id="work-2")
    resumed_request.agentic_identity = first.agentic_identity
    resumed = backend.execute(resumed_request)

    assert resumed.status == "SUCCESS"
    assert resumed.agentic_identity.session_or_thread_id == "conv-fake-1"
    assert resumed.agentic_identity.last_successful_turn == 2
    assert resumed.agentic_identity.completed_work_unit_ids == ["work-1", "work-2"]
    assert adapter.invocations[-1]["conversation_id"] == "conv-fake-1"
    assert adapter.invocations[-1]["continue_conversation"] is True


def test_stale_workspace_session_is_not_invoked(tmp_path):
    source_sha = _repo(tmp_path)
    adapter = FakeAntigravityAdapter()
    backend = _backend(adapter)
    first = backend.execute(_request(tmp_path, source_sha))
    prior_count = len(adapter.invocations)
    (tmp_path / "base.txt").write_text("fallback changed workspace\n", encoding="utf-8")

    request = _request(tmp_path, source_sha, task_id="work-2")
    request.agentic_identity = first.agentic_identity
    result = backend.execute(request)

    assert result.status == "DEGRADED"
    assert result.sanitized_errors == ["STALE_AGENT_SESSION:WORKSPACE_CHANGED"]
    assert len(adapter.invocations) == prior_count


def test_quota_loss_is_structured_nonterminal_and_contains_no_raw_error(tmp_path):
    source_sha = _repo(tmp_path)

    class Exhausted(FakeAntigravityAdapter):
        def execute_prompt(self, *args, **kwargs):
            return AntigravityResponse(
                conversation_id="conv-quota",
                status=AntigravityStatus.ERROR,
                mapped_aos_status=RunStatus.FAILED,
                raw_response="raw secret response",
                error_message="429 ResourceExhausted raw secret",
            )

    result = _backend(Exhausted()).execute(_request(tmp_path, source_sha))

    assert result.status == "DEGRADED"
    assert result.availability.state.value == "QUOTA_EXHAUSTED"
    serialized = json.dumps(result.to_dict())
    assert "raw secret" not in serialized


def test_write_scope_is_verified_after_agentic_turn(tmp_path):
    source_sha = _repo(tmp_path)

    class Writer(FakeAntigravityAdapter):
        def execute_prompt(self, *args, **kwargs):
            (tmp_path / "outside.txt").write_text("escape\n", encoding="utf-8")
            return super().execute_prompt(*args, **kwargs)

    request = _request(tmp_path, source_sha, write_scope=["allowed"])
    result = _backend(Writer()).execute(request)

    assert result.status == "FAILED"
    assert result.sanitized_errors == ["ANTIGRAVITY_WRITE_SCOPE_VIOLATION"]


def test_supervisor_supersedes_stale_session_and_waits_without_project_failure(tmp_path):
    source_sha = _repo(tmp_path)
    adapter = FakeAntigravityAdapter()
    backend = _backend(adapter)
    first = backend.execute(_request(tmp_path, source_sha))
    identity = first.agentic_identity
    registry = AgentRunRegistry()
    run = registry.create_run(
        project_id="project-1",
        run_type="AGENTIC",
        authority_id="AUTH-1",
        controller_id="controller-1",
        agent_provider="antigravity",
        workspace_path=str(tmp_path),
        source_sha=source_sha,
        checkpoint_id="checkpoint-1",
        objective_id="objective-1",
    )
    registry.update_run_metadata(run.run_id, {
        "resource_id": identity.resource_id,
        "backend_id": identity.backend_id,
        "session_or_thread_id": identity.session_or_thread_id,
        "agent_conversation_id": identity.session_or_thread_id,
        "workspace_fingerprint": identity.workspace_fingerprint,
        "adapter_contract_version": identity.adapter_contract_version,
        "backend_version": identity.backend_version,
        "executable_sha256": identity.executable_sha256,
        "auth_mode": identity.auth_mode,
        "last_successful_turn": identity.last_successful_turn,
        "completed_work_unit_ids": identity.completed_work_unit_ids,
        "completed_work_unit_signatures": identity.completed_work_unit_signatures,
    })
    registry.transition(run.run_id, RunStatus.STARTING)
    registry.transition(run.run_id, RunStatus.RUNNING)
    registry.transition(run.run_id, RunStatus.INTERRUPTED)
    (tmp_path / "base.txt").write_text("fallback mutation\n", encoding="utf-8")
    supervisor = ParallelSupervisor(
        registry,
        router=ExecutionRouter([backend], ag_required=True),
    )

    resumed = supervisor.resume_run(run.run_id, "Continue remaining work")

    assert resumed.status == RunStatus.WAITING_AGENT
    assert resumed.session_or_thread_id is None
    assert resumed.superseded_session_ids == [identity.session_or_thread_id]


def test_unproven_backend_is_registered_but_never_invoked(tmp_path):
    source_sha = _repo(tmp_path)
    adapter = FakeAntigravityAdapter()
    backend = AntigravityAgenticExecutionBackend(
        adapter,
        capability_status_provider=lambda: "UNPROVEN",
    )
    result = backend.execute(_request(tmp_path, source_sha))

    assert result.status == "DEGRADED"
    assert result.availability.state.value == "CONTRACT_FAILURE"
    assert adapter.invocations == []


def test_autonomous_host_registers_first_class_antigravity_outside_provider_factories(tmp_path):
    from aos.autonomous_host import _PROVIDER_FACTORIES, build_execution_router

    policy = Path(__file__).resolve().parents[3] / "descriptors" / "nemotron.planner-policy.json"
    router = build_execution_router(policy, tmp_path / "runtime")

    backend = router.get_backend("antigravity")
    assert isinstance(backend, AntigravityAgenticExecutionBackend)
    assert backend.cost.value == "SUBSCRIPTION_INCLUDED"
    assert "antigravity" not in _PROVIDER_FACTORIES


def test_interrupt_delegates_to_owned_adapter_process():
    class Interruptible(FakeAntigravityAdapter):
        interrupted = False

        def interrupt(self):
            self.interrupted = True

    adapter = Interruptible()
    backend = _backend(adapter)
    backend.interrupt("execution-1")
    assert adapter.interrupted is True
