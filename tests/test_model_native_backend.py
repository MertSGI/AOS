"""Focused qualification tests for ModelNativeImplementationBackend."""
import hashlib
import json
from pathlib import Path
import pytest

from extensions.autonomy_fabric.execution_backend import (
    ExecutionCapability,
    ExecutionCost,
    ExecutionRequest,
)
from extensions.autonomy_fabric.model_native_backend import (
    ModelNativeImplementationBackend,
    _resolve_zero_cost_api_key,
)
from extensions.autonomy_fabric.resource_orchestrator import ResourceOrchestrator


def test_zero_cost_api_key_resolver():
    """Verify that zero-cost NVIDIA API key resolves from environment or local providers.json."""
    key = _resolve_zero_cost_api_key()
    assert key is not None
    assert key.startswith("nvapi-")


def test_resource_orchestrator_selects_model_native_backend():
    """Verify that ResourceOrchestrator selects ModelNativeImplementationBackend for file repair."""
    backend = ModelNativeImplementationBackend()
    assert backend.cost == ExecutionCost.FREE_TIER_CLOUD

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
    ranks = orch.rank([backend], req)
    assert len(ranks) == 1
    assert ranks[0].eligible is True
    assert orch.select([backend], req) == "model_native_implementation_worker"


def test_model_native_worker_bounded_replacement_with_mock(tmp_path: Path):
    """Verify single authorized file replacement and functional test execution."""
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

    # Mock model that returns correct replacement
    def fake_model(rel_path, orig_sha, orig_content, instruction):
        assert rel_path == "src/calc.py"
        assert orig_sha == orig_sha256
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": orig_sha,
            "new_content": "def subtract(a, b):\n    return a - b\n",
        }

    orig_sha256 = orig_sha
    backend = ModelNativeImplementationBackend(model_caller=fake_model)

    req = ExecutionRequest(
        task_id="repair-task-1",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={
            "prompt": "Fix subtract function in src/calc.py",
            "tests": ["python test_calc.py"],
            "runtime_dir": str(tmp_path / "runtime"),
        },
    )

    res = backend.execute(req)
    assert res.status == "SUCCESS"
    assert res.exit_code == 0
    assert "src/calc.py" in res.changed_paths
    assert target.read_text(encoding="utf-8") == "def subtract(a, b):\n    return a - b\n"
    new_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert res.artifact_hashes["src/calc.py"] == new_sha
    assert res.evidence_payload["receipt"] is not None


def test_model_native_worker_rejects_extra_fields(tmp_path: Path):
    """Negative regression: Model returning extra / unknown fields is rejected fail-closed."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

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
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": []},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "STRUCTURED_SCHEMA_VIOLATION" in res.sanitized_errors[0]
    # File must not have been mutated
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_model_native_worker_rejects_path_drift(tmp_path: Path):
    """Negative regression: Model attempting to modify an unauthorized file is rejected."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

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
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": []},
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

    def fake_model(rel_path, orig_sha, orig_content, instruction):
        return {
            "relative_path": "src/calc.py",
            "expected_original_sha256": "0" * 64,  # wrong hash
            "new_content": "x = 2\n",
        }

    backend = ModelNativeImplementationBackend(model_caller=fake_model)
    req = ExecutionRequest(
        task_id="repair-task-4",
        project_id="canary-proj",
        workspace=str(tmp_path),
        operation_class="AGENTIC",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="CANARY-AUTH-01",
        write_scope=["src/calc.py"],
        payload={"prompt": "Fix x", "tests": []},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "PRECONDITION_HASH_MISMATCH" in res.sanitized_errors[0]


def test_model_native_worker_reverts_on_test_failure(tmp_path: Path):
    """Negative regression: If pre-authorized test fails on candidate code, changes are reverted."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "calc.py"
    target.write_text("x = 1\n", encoding="utf-8")
    orig_sha = hashlib.sha256(target.read_bytes()).hexdigest()

    test_file = tmp_path / "test_calc.py"
    test_file.write_text("import sys\nassert False, 'Always fail'\n", encoding="utf-8")

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
        required_capabilities=[ExecutionCapability.FILE_WRITE],
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
