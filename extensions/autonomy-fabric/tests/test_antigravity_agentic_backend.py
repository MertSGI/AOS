import json
import subprocess
from pathlib import Path

from extensions.autonomy_fabric.antigravity_adapter import (
    AntigravityCLIAdapter,
    AntigravityResponse,
    AntigravityStatus,
    FakeAntigravityAdapter,
)
from extensions.autonomy_fabric import antigravity_agentic_backend as backend_mod
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


# Regression Tests A-G for Runtime-Owned Root Lock
def test_regression_a_untracked_root_lock_not_returned_by_changed_paths(tmp_path):
    _repo(tmp_path)
    lock_file = tmp_path / ".aos_workspace_active.lock"
    lock_file.write_bytes(b"0")

    changed = AntigravityAgenticExecutionBackend._changed_paths(str(tmp_path))
    assert ".aos_workspace_active.lock" not in changed
    assert changed == []


def test_regression_b_actively_held_root_lock_succeeds(tmp_path):
    from aos.runtime_store import exclusive_file_lock

    source_sha = _repo(tmp_path)
    lock_file = tmp_path / ".aos_workspace_active.lock"
    lock_file.write_bytes(b"0")

    with exclusive_file_lock(lock_file):
        changed = AntigravityAgenticExecutionBackend._changed_paths(str(tmp_path))
        assert changed == []

        adapter = FakeAntigravityAdapter()
        backend = _backend(adapter)
        req = _request(tmp_path, source_sha, write_scope=[])
        result = backend.execute(req)
        assert result.status == "SUCCESS"
        assert ".aos_workspace_active.lock" not in result.changed_paths


def test_regression_c_different_untracked_product_file_remains_visible(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".aos_workspace_active.lock").write_bytes(b"0")
    (tmp_path / "new_product.py").write_text("print('hello')\n", encoding="utf-8")

    changed = AntigravityAgenticExecutionBackend._changed_paths(str(tmp_path))
    assert ".aos_workspace_active.lock" not in changed
    assert "new_product.py" in changed
    assert changed == ["new_product.py"]


def test_regression_d_tracked_lock_named_file_remains_visible_when_modified(tmp_path):
    _repo(tmp_path)
    lock_file = tmp_path / ".aos_workspace_active.lock"
    lock_file.write_text("initial lock content\n", encoding="utf-8")
    subprocess.run(["git", "add", ".aos_workspace_active.lock"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "track lock file"], cwd=tmp_path, check=True)

    # Modify the tracked lock file
    lock_file.write_text("modified lock content\n", encoding="utf-8")

    changed = AntigravityAgenticExecutionBackend._changed_paths(str(tmp_path))
    assert ".aos_workspace_active.lock" in changed


def test_regression_e_nested_lock_named_file_remains_visible(tmp_path):
    _repo(tmp_path)
    (tmp_path / ".aos_workspace_active.lock").write_bytes(b"0")
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir(parents=True)
    nested_lock = nested_dir / ".aos_workspace_active.lock"
    nested_lock.write_bytes(b"0")

    changed = AntigravityAgenticExecutionBackend._changed_paths(str(tmp_path))
    assert ".aos_workspace_active.lock" not in changed
    assert "nested/.aos_workspace_active.lock" in changed


def test_regression_f_planning_bridge_with_lock_held_returns_success_empty_changed_paths(tmp_path):
    from aos.runtime_store import exclusive_file_lock
    from extensions.autonomy_fabric.agentic_planning_bridge import AgenticStructuredPlanningBridge

    source_sha = _repo(tmp_path)
    lock_file = tmp_path / ".aos_workspace_active.lock"
    lock_file.write_bytes(b"0")

    valid_json = '{"answer": "valid plan"}'
    adapter = FakeAntigravityAdapter()
    adapter.default_status = AntigravityStatus.SUCCESS
    # Return response containing JSON
    cid = "conv-planning-1"
    adapter.set_canned_response(
        cid,
        AntigravityResponse(
            conversation_id=cid,
            status=AntigravityStatus.SUCCESS,
            mapped_aos_status=RunStatus.COMPLETED,
            raw_response=f"Here is the plan:\n```json\n{valid_json}\n```",
            parsed_json={"conversation_id": cid, "status": "SUCCESS", "response": valid_json},
            duration_seconds=0.1,
            turn_count=1,
        ),
    )
    class PlanningAntigravityBackend(AntigravityAgenticExecutionBackend):
        def _run(self, request, context_pack, prior):
            res = super()._run(request, context_pack, prior)
            if res.status == "SUCCESS":
                res.stdout_digest = valid_json
                res.evidence_payload["raw_output"] = valid_json
            return res

    backend = PlanningAntigravityBackend(
        adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity=IDENTITY,
    )
    bridge = AgenticStructuredPlanningBridge(backend)

    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }

    req = ExecutionRequest(
        task_id="plan-with-lock",
        project_id="test-ag-planning",
        workspace=str(tmp_path),
        operation_class="MODEL_REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="AUTH-PLAN-1",
        payload={
            "prompt": "Produce execution plan",
            "schema": schema,
            "source_sha": source_sha,
            "conversation_id": cid,
        },
    )

    with exclusive_file_lock(lock_file):
        res = bridge.execute(req)
        assert res.status == "SUCCESS"
        assert res.changed_paths == []
        assert "ANTIGRAVITY_WRITE_SCOPE_VIOLATION" not in res.sanitized_errors
        assert res.evidence_payload["proposal"] == {"answer": "valid plan"}


def test_regression_g_unrelated_workspace_mutation_fails_closed(tmp_path):
    source_sha = _repo(tmp_path)
    (tmp_path / ".aos_workspace_active.lock").write_bytes(b"0")

    adapter = FakeAntigravityAdapter()
    backend = _backend(adapter)

    class MutatingBackend(AntigravityAgenticExecutionBackend):
        def _run(self, request, context_pack, prior):
            # Mutate unauthorized file during execution
            (Path(request.workspace) / "unauthorized_mutation.txt").write_text("mutated\n", encoding="utf-8")
            return super()._run(request, context_pack, prior)

    mutating_backend = MutatingBackend(
        adapter,
        capability_status_provider=lambda: "TEST_DOUBLE",
        executable_identity=IDENTITY,
    )

    req = _request(tmp_path, source_sha, write_scope=["allowed_dir/"])
    res = mutating_backend.execute(req)

    assert res.status == "FAILED"
    assert "ANTIGRAVITY_WRITE_SCOPE_VIOLATION" in res.sanitized_errors


# Regression Tests for Managed CLI Identity Discovery (A through F)
def test_identity_regression_a_injected_identity_preserved():
    custom_identity = {
        "path": "/custom/injected/antigravity",
        "filename": "antigravity",
        "sha256": "b" * 64,
        "version": "2.0.0",
    }
    backend = AntigravityAgenticExecutionBackend(executable_identity=custom_identity)
    assert backend._identity() == custom_identity


def test_identity_regression_b_managed_executable_resolved_when_agy_absent(monkeypatch, tmp_path):
    fake_managed = tmp_path / "antigravity.exe"
    fake_managed.write_text("binary content", encoding="utf-8")

    monkeypatch.setattr("shutil.which", lambda cmd, **kwargs: None)
    monkeypatch.setattr(
        AntigravityCLIAdapter,
        "discover_cli_binary",
        staticmethod(lambda *args, **kwargs: str(fake_managed)),
    )
    monkeypatch.setattr(
        backend_mod,
        "resolve_executable_identity",
        lambda path, **kwargs: {
            "path": str(fake_managed),
            "filename": "antigravity.exe",
            "sha256": "c" * 64,
            "version": "1.2.10",
        } if path == str(fake_managed) else None,
    )

    backend = AntigravityAgenticExecutionBackend()
    identity = backend._identity()
    assert identity is not None
    assert identity["path"] == str(fake_managed)
    assert identity["filename"] == "antigravity.exe"


def test_identity_regression_c_matching_attestation_yields_available_with_managed_discovery(monkeypatch, tmp_path):
    fake_managed = tmp_path / "antigravity.exe"
    fake_managed.write_text("binary content", encoding="utf-8")
    expected_identity = {
        "path": str(fake_managed),
        "filename": "antigravity.exe",
        "sha256": "d" * 64,
        "version": "1.2.10",
    }

    monkeypatch.setattr(
        AntigravityCLIAdapter,
        "discover_cli_binary",
        staticmethod(lambda *args, **kwargs: str(fake_managed)),
    )
    monkeypatch.setattr(
        backend_mod,
        "resolve_executable_identity",
        lambda path, **kwargs: expected_identity if path == str(fake_managed) else None,
    )
    monkeypatch.setattr(
        backend_mod,
        "resolve_capability_status",
        lambda path, identity=None, **kwargs: "PROVEN" if identity == expected_identity else "UNPROVEN",
    )

    backend = AntigravityAgenticExecutionBackend()
    availability = backend.get_availability()
    assert availability.state.value == "AVAILABLE"
    assert availability.evidence["capability_status"] == "PROVEN"


def test_identity_regression_d_discovery_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(
        AntigravityCLIAdapter,
        "discover_cli_binary",
        staticmethod(lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("CLI_BINARY_NOT_FOUND"))),
    )

    backend = AntigravityAgenticExecutionBackend()
    assert backend._identity() is None
    assert backend._capability_status() == "UNPROVEN"
    availability = backend.get_availability()
    assert availability.state.value == "CONTRACT_FAILURE"


def test_identity_regression_e_adapter_and_backend_identity_refer_to_same_binary(monkeypatch, tmp_path):
    fake_managed = tmp_path / "antigravity.exe"
    fake_managed.write_text("binary content", encoding="utf-8")
    resolved_id = {
        "path": str(fake_managed),
        "filename": "antigravity.exe",
        "sha256": "e" * 64,
        "version": "1.2.10",
    }

    monkeypatch.setattr(
        AntigravityCLIAdapter,
        "discover_cli_binary",
        staticmethod(lambda *args, **kwargs: str(fake_managed)),
    )
    monkeypatch.setattr(
        backend_mod,
        "resolve_executable_identity",
        lambda path, **kwargs: resolved_id if path == str(fake_managed) else None,
    )

    backend = AntigravityAgenticExecutionBackend()
    identity = backend._identity()
    adapter = backend._adapter_instance()

    assert identity is not None
    assert adapter.cli_binary_path == identity["path"]


def test_identity_regression_f_no_literal_agy_dependency_in_backend(monkeypatch, tmp_path):
    fake_managed = tmp_path / "antigravity.exe"
    fake_managed.write_text("binary content", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        AntigravityCLIAdapter,
        "discover_cli_binary",
        staticmethod(lambda *args, **kwargs: str(fake_managed)),
    )
    def fake_resolve_identity(path, **kwargs):
        calls.append(("identity", path))
        return {
            "path": str(fake_managed),
            "filename": "antigravity.exe",
            "sha256": "f" * 64,
            "version": "1.2.10",
        }
    def fake_resolve_capability(path, identity=None, **kwargs):
        calls.append(("capability", path))
        return "PROVEN"

    monkeypatch.setattr(
        backend_mod,
        "resolve_executable_identity",
        fake_resolve_identity,
    )
    monkeypatch.setattr(
        backend_mod,
        "resolve_capability_status",
        fake_resolve_capability,
    )

    backend = AntigravityAgenticExecutionBackend()
    backend.get_availability()

    for call_type, target in calls:
        assert target != "agy", f"Literal 'agy' passed to {call_type}"
        assert target == str(fake_managed)


