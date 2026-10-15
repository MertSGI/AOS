"""Focused qualification and regression tests for ModelNativeImplementationBackend."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest

from extensions.autonomy_fabric.execution_backend import (
    ExecutionAvailabilityState,
    ExecutionCapability,
    ExecutionCost,
    ExecutionHealth,
    ExecutionRequest,
)
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
    _resolve_and_validate_test_command,
    _resolve_zero_cost_api_key,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


def _init_git_workspace(workspace: Path) -> str:
    """Initialize a git repository with an initial commit to establish verifiable lineage."""
    subprocess.run(["git", "init"], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "AOS Test Worker"], cwd=str(workspace), check=True)
    subprocess.run(["git", "config", "user.email", "test-worker@aos.invalid"], cwd=str(workspace), check=True)
    subprocess.run(["git", "add", "."], cwd=str(workspace), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit", "--allow-empty"], cwd=str(workspace), check=True)
    head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace), capture_output=True, text=True, check=True)
    return head_proc.stdout.strip().lower()


def test_zero_cost_api_key_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """CI Failure 1 Fix: Verify deterministic credentials and fail-closed without credentials."""
    # 1. Test resolution from NVIDIA_API_KEY environment variable
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-deterministic-key-env")
    key = _resolve_zero_cost_api_key()
    assert key == "nvapi-test-deterministic-key-env"

    # 2. Test resolution from local cline providers.json configuration
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    fake_home = tmp_path / "fake_home"
    fake_providers_file = fake_home / ".cline" / "data" / "settings" / "providers.json"
    fake_providers_file.parent.mkdir(parents=True, exist_ok=True)
    fake_providers_file.write_text(
        json.dumps({
            "providers": {
                "openai-compatible": {
                    "settings": {
                        "apiKey": "nvapi-test-deterministic-key-file"
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    key_file = _resolve_zero_cost_api_key()
    assert key_file == "nvapi-test-deterministic-key-file"

    # 3. Verify missing credentials fail closed (never assume live credentials in CI)
    fake_empty_home = tmp_path / "empty_home"
    fake_empty_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_empty_home)
    key_none = _resolve_zero_cost_api_key()
    assert key_none is None


def test_resource_orchestrator_selects_model_native_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """CI Failure 2 Fix: Verify availability state enum and routing with/without eligible free provider."""
    # Scenario A: Eligible free provider present (via mock caller)
    backend_eligible = ModelNativeImplementationBackend(
        model_caller=lambda p, s, c, i: {"relative_path": p, "expected_original_sha256": s, "new_content": c}
    )
    assert backend_eligible.cost == ExecutionCost.FREE_TIER_CLOUD
    assert backend_eligible.get_health() == ExecutionHealth.HEALTHY
    avail_eligible = backend_eligible.get_availability()
    assert avail_eligible.state == ExecutionAvailabilityState.AVAILABLE

    req = ExecutionRequest(
        task_id="task-repair-1",
        project_id="test-proj",
        workspace=".",
        operation_class="AGENTIC",
        required_capabilities=[
            ExecutionCapability.FILE_WRITE,
            ExecutionCapability.TEST_EXECUTION,
        ],
        authority_id="AUTH-01",
    )

    orch = ResourceOrchestrator()
    ranks = orch.rank([backend_eligible], req)
    assert len(ranks) == 1
    assert ranks[0].eligible is True
    assert orch.select([backend_eligible], req) == "model_native_implementation_worker"

    # Scenario B: No eligible free provider present (no API key, no mock caller)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    fake_empty_home = tmp_path / "empty_home_orch"
    fake_empty_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: fake_empty_home)

    backend_unavail = ModelNativeImplementationBackend()
    assert backend_unavail.get_health() == ExecutionHealth.UNAVAILABLE
    avail_unavail = backend_unavail.get_availability()
    assert avail_unavail.state == ExecutionAvailabilityState.AUTH_UNAVAILABLE

    ranks_unavail = orch.rank([backend_unavail], req)
    assert len(ranks_unavail) == 1
    assert ranks_unavail[0].eligible is False
    assert "HEALTH_UNAVAILABLE" in ranks_unavail[0].reasons or "AVAILABILITY_AUTH_UNAVAILABLE" in ranks_unavail[0].reasons
    assert orch.select([backend_unavail], req) is None


def test_model_native_worker_bounded_replacement_with_mock(tmp_path: Path):
    """CI Failure 3 & 4 Fix: Verified argv test execution, git lineage integrity, and receipt."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("def subtract(a, b):\n    return a + b\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    test_file = tmp_path / "test_calc.py"
    test_file.write_text(
        "import sys\nfrom src.calc import subtract\nassert subtract(5, 3) == 2, 'Failed'\n",
        encoding="utf-8",
    )

    # Initialize git repo to provide valid base commit lineage
    base_git_sha = _init_git_workspace(tmp_path)

    # Mock model that returns correct replacement
    orig_sha256 = orig_sha
    def fake_model(rel_path, orig_sha, orig_content, instruction):
        assert rel_path == "src/calc.py"
        assert orig_sha == orig_sha256
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": orig_sha,
            "new_content": "def subtract(a, b):\n    return a - b\n",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)

    req = ExecutionRequest(
        task_id="repair-task-1",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={
            "prompt": "Fix subtract function in src/calc.py",
            "tests": ["python test_calc.py"],
            "runtime_dir": str(tmp_path / "runtime"),
            "source_sha": base_git_sha,
        },
    )

    res = backend.execute(req)
    assert res.status == "SUCCESS", f"Execution failed: {res.sanitized_errors}"
    assert res.exit_code == 0
    assert "src/calc.py" in res.changed_paths
    assert target.read_text(encoding="utf-8") == "def subtract(a, b):\n    return a - b\n"
    new_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert res.artifact_hashes["src/calc.py"] == new_sha
    assert res.evidence_payload["receipt"] is not None
    receipt = res.evidence_payload["receipt"]
    assert receipt["base_sha"] == base_git_sha
    assert receipt["result_sha"] != base_git_sha
    assert len(receipt["result_sha"]) == 40


def test_model_native_worker_reverts_on_test_failure(tmp_path: Path):
    """Negative regression: If pre-authorized test fails on candidate code, changes are reverted."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("import sys\nassert False, 'Always fail'\n", encoding="utf-8")

    _init_git_workspace(tmp_path)

    def fake_model(rel_path, orig_sha, orig_content, instruction):
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": orig_sha,
            "new_content": "x = 2\n",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)
    req = ExecutionRequest(
        task_id="repair-task-5",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "FUNCTIONAL_TEST_FAILED" in res.sanitized_errors[0]
    # Verify file was reverted to original
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == orig_sha


def test_model_native_worker_rejects_extra_fields(tmp_path: Path):
    """Negative regression: Model returning extra / unknown fields is rejected fail-closed."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    _init_git_workspace(tmp_path)

    def fake_model(rel_path, orig_sha, orig_content, instruction):
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": orig_sha,
            "new_content": "x = 2\n",
            "extra_unauthorized_key": "malicious",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)
    req = ExecutionRequest(
        task_id="repair-task-2",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "STRUCTURED_SCHEMA_VIOLATION" in res.sanitized_errors[0]
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_model_native_worker_rejects_path_drift(tmp_path: Path):
    """Negative regression: Model attempting to modify an unauthorized file is rejected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    _init_git_workspace(tmp_path)

    def fake_model(rel_path, orig_sha, orig_content, instruction):
        return {
            "relative_path": "src/other.py",
            "expected_original_sha256": orig_sha,
            "new_content": "x = 2\n",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)
    req = ExecutionRequest(
        task_id="repair-task-3",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "UNAUTHORIZED_PATH_MUTATION" in res.sanitized_errors[0]


def test_model_native_worker_rejects_hash_mismatch(tmp_path: Path):
    """Negative regression: Model generating against wrong precondition hash is rejected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    _init_git_workspace(tmp_path)

    def fake_model(rel_path, orig_sha, orig_content, instruction):
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": "0" * 64,
            "new_content": "x = 2\n",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)
    req = ExecutionRequest(
        task_id="repair-task-4",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "PRECONDITION_HASH_MISMATCH" in res.sanitized_errors[0]


def test_model_native_worker_rejects_empty_functional_tests(tmp_path: Path):
    """Gap B.2 Negative regression: Mutating execution without pre-authorized functional tests fails closed."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    _init_git_workspace(tmp_path)

    backend = ModelNativeImplementationBackend(model_caller=lambda p, s, c, i: {"relative_path": p, "expected_original_sha256": s, "new_content": "x = 2\n"})
    req = ExecutionRequest(
        task_id="repair-task-no-tests",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": []},  # Empty tests list
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "NO_FUNCTIONAL_TESTS" in res.sanitized_errors[0]
    # Verify file was never modified
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_model_native_worker_rejects_long_horizon_agentic_work(tmp_path: Path):
    """Gap B.1 Negative regression: Requests demanding LONG_HORIZON_AGENTIC_WORK are rejected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    backend = ModelNativeImplementationBackend()
    assert ExecutionCapability.LONG_HORIZON_AGENTIC_WORK not in backend.supported_capabilities

    req = ExecutionRequest(
        task_id="task-long-horizon",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.LONG_HORIZON_AGENTIC_WORK],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "UNSUPPORTED_CAPABILITY" in res.sanitized_errors[0]


def test_model_native_worker_rejects_unchanged_source(tmp_path: Path):
    """Gap B.3 Negative regression: Model returning unchanged source is rejected without commit or receipt."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("assert True\n", encoding="utf-8")
    _init_git_workspace(tmp_path)

    # Model returns exact same content
    backend = ModelNativeImplementationBackend(
        model_caller=lambda p, s, c, i: {"relative_path": p, "expected_original_sha256": s, "new_content": c}
    )
    req = ExecutionRequest(
        task_id="repair-task-unchanged",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "UNCHANGED_SOURCE" in res.sanitized_errors[0]


def test_model_native_worker_rejects_unverifiable_git_lineage(tmp_path: Path):
    """Gap B.3 Negative regression: Non-git workspace is rejected fail-closed before mutation."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("assert True\n", encoding="utf-8")

    # DO NOT init git repo - workspace lacks git lineage
    backend = ModelNativeImplementationBackend(
        model_caller=lambda p, s, c, i: {"relative_path": p, "expected_original_sha256": s, "new_content": "x = 2\n"}
    )
    req = ExecutionRequest(
        task_id="repair-task-no-git",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE, ExecutionCapability.TEST_EXECUTION],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": ["python test_calc.py"]},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "GIT_LINEAGE_UNVERIFIABLE" in res.sanitized_errors[0]
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_command_argv_safety(tmp_path: Path):
    """Test command validation rejects shell metatokens, inline Python flags, and missing files."""
    valid_test = tmp_path / "test_ok.py"
    valid_test.write_text("assert True\n", encoding="utf-8")

    # 1. Valid python test specification converts portably to sys.executable
    argv = _resolve_and_validate_test_command("python test_ok.py", tmp_path)
    assert argv == [sys.executable, "test_ok.py"]

    # 2. Valid pytest specification converts to -m pytest
    argv_pytest = _resolve_and_validate_test_command("pytest test_ok.py", tmp_path)
    assert argv_pytest == [sys.executable, "-m", "pytest", "test_ok.py"]

    # 3. Disallowed shell metacharacters raise ValueError
    for bad_cmd in [
        "python test_ok.py; rm -rf /",
        "python test_ok.py && echo bad",
        "python test_ok.py | grep fail",
        "python test_ok.py > out.txt",
    ]:
        with pytest.raises(ValueError, match="SHELL_METATOKEN_DISALLOWED"):
            _resolve_and_validate_test_command(bad_cmd, tmp_path)

    # 4. Disallowed inline execution flags raise ValueError
    with pytest.raises(ValueError, match="DISALLOWED_PYTHON_FLAG"):
        _resolve_and_validate_test_command(["python", "-c", "import os"], tmp_path)

    # 5. Unauthorized binary raises ValueError
    with pytest.raises(ValueError, match="UNAUTHORIZED_TEST_EXECUTABLE"):
        _resolve_and_validate_test_command("bash test.sh", tmp_path)

    # 6. Non-existent test target raises ValueError
    with pytest.raises(ValueError, match="TEST_TARGET_NOT_FOUND"):
        _resolve_and_validate_test_command("python missing_test.py", tmp_path)
