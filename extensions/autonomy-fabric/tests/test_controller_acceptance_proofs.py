"""Comprehensive Controller Acceptance Suite for AOS Native Execution Fabric V2.

Verifies:
- Quota Exhaustion Proof (Section 9)
- AG Binary Absence Proof (Section 10)
- Persistent Coordinator Real Restart & Re-execution Proof (Section 11)
- Authority Fail-Closed Proof (Section 12)
- Secret Redaction Adversarial Proof (Section 13)
- Browser Execution Backend Proof (Section 16)
- Model Reasoning Backend Route Proof (Section 17)
"""

import pytest
import os
import sys
import tempfile
import subprocess
from unittest.mock import MagicMock

from extensions.autonomy_fabric.run_registry import AgentRunRegistry, RunStatus
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    ExecutionHealth,
    ExecutionCost,
    EvidenceClass,
)
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import (
    NativeFileWorker,
    NativeProcessWorker,
    NativeGitWorker,
    GitHubCIWorker,
    BrowserExecutionBackend,
    ModelReasoningBackend,
    AntigravityExecutionBackend,
    redact_secrets,
)
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator
from extensions.autonomy_fabric.authority_router import AuthorityRouter


# -------------------------------------------------------------
# Section 9: Quota Exhaustion Proof
# -------------------------------------------------------------
def test_quota_exhaustion_proof():
    """When AG is quota-exhausted, router fails over to Native without GLOBAL_PROGRAM_HOLD."""
    ag_exhausted = AntigravityExecutionBackend(simulate_exhausted=True)
    native_proc = NativeProcessWorker()

    router = ExecutionRouter(backends=[ag_exhausted, native_proc])

    req = ExecutionRequest(
        task_id="t-quota-test",
        project_id="AOS",
        workspace=".",
        operation_class="TEST_EXEC",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="AOS-AUTH-01",
        payload={"cmd": ["python", "-c", "print('quota-failover-pass')"]},
    )

    result = router.execute_with_failover(req)
    assert result.status == "SUCCESS"
    assert result.backend_id == "native_process_worker"
    assert "quota-failover-pass" in result.stdout_digest


# -------------------------------------------------------------
# Section 10: AG Binary Absence Proof
# -------------------------------------------------------------
def test_ag_binary_absence_proof():
    """AOS starts, selects tasks, and executes natively when AG binary is unavailable."""
    ag_unavailable = AntigravityExecutionBackend(simulate_unavailable=True)
    assert ag_unavailable.get_health() == ExecutionHealth.UNAVAILABLE

    native_file = NativeFileWorker()
    native_proc = NativeProcessWorker()
    router = ExecutionRouter(backends=[ag_unavailable, native_file, native_proc], ag_required=False)

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = AgentRunRegistry()
        dag = TaskDAG("proj-no-ag", registry)
        n1 = dag.add_node("node-1", "DEV", "auth-1")
        n1.payload = {"action": "write_file", "path": "test.txt", "content": "hello\n"}

        coord = PersistentCoordinator(
            project_id="proj-no-ag",
            workspace_path=tmpdir,
            dag=dag,
            router=router,
            registry=registry,
        )

        results = coord.execute_next_batch(max_tasks=1)
        assert len(results) == 1
        assert results[0].status == "SUCCESS"
        assert results[0].backend_id == "native_file_worker"
        assert "node-1" in coord.state.completed_task_ids


# -------------------------------------------------------------
# Section 11: Persistent Coordinator Real Proof
# -------------------------------------------------------------
def test_persistent_coordinator_real_proof():
    """Proves completed nodes are not re-executed, pending nodes resume, zero mutation/evidence duplication."""
    with tempfile.TemporaryDirectory() as tmpdir:
        checkpoint_path = os.path.join(tmpdir, "coord_state.json")
        registry = AgentRunRegistry()
        dag = TaskDAG("proj-restart-proof", registry)

        n1 = dag.add_node("n1", "DEV", "auth-1")
        n2 = dag.add_node("n2", "DEV", "auth-1", dependencies=["n1"])

        file_worker = NativeFileWorker()
        router = ExecutionRouter(backends=[file_worker])

        file1 = os.path.join(tmpdir, "f1.txt")
        file2 = os.path.join(tmpdir, "f2.txt")
        n1.payload = {"action": "write_file", "path": "f1.txt", "content": "first\n"}
        n2.payload = {"action": "write_file", "path": "f2.txt", "content": "second\n"}

        # Subprocess script to execute exactly 1 batch of the coordinator
        worker_script = f"""
import sys
sys.path.insert(0, {repr(os.path.abspath('.'))})
from extensions.autonomy_fabric.run_registry import AgentRunRegistry
from extensions.autonomy_fabric.task_dag import TaskDAG
from extensions.autonomy_fabric.execution_router import ExecutionRouter
from extensions.autonomy_fabric.native_workers import NativeFileWorker
from extensions.autonomy_fabric.persistent_coordinator import PersistentCoordinator

registry = AgentRunRegistry()
dag = TaskDAG("proj-restart-proof", registry)
n1 = dag.add_node("n1", "DEV", "auth-1")
n2 = dag.add_node("n2", "DEV", "auth-1", dependencies=["n1"])
n1.payload = {{"action": "write_file", "path": "f1.txt", "content": "first\\n"}}
n2.payload = {{"action": "write_file", "path": "f2.txt", "content": "second\\n"}}

router = ExecutionRouter(backends=[NativeFileWorker()])
coord = PersistentCoordinator(
    project_id="proj-restart-proof",
    workspace_path={repr(tmpdir)},
    dag=dag,
    router=router,
    registry=registry,
    checkpoint_file={repr(checkpoint_path)},
)
res = coord.execute_next_batch(max_tasks=1)
print("EXECUTED:", [r.task_id for r in res])
"""
        # Process A: Runs node n1 in a separate OS process, writes checkpoint, and terminates
        proc_a = subprocess.run(
            [sys.executable, "-c", worker_script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "EXECUTED: ['n1']" in proc_a.stdout
        assert os.path.exists(file1)

        # Process B: Starts in an independent OS process, resumes checkpoint, runs node n2, and terminates
        proc_b = subprocess.run(
            [sys.executable, "-c", worker_script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "EXECUTED: ['n2']" in proc_b.stdout
        assert os.path.exists(file2)

        # Process C: Starts in another independent process; since both are completed, 0 nodes are re-executed
        proc_c = subprocess.run(
            [sys.executable, "-c", worker_script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "EXECUTED: []" in proc_c.stdout


# -------------------------------------------------------------
# Section 12: Authority Fail-Closed Proof
# -------------------------------------------------------------
def test_authority_fail_closed_proof():
    """All sensitive or out-of-boundary operations are denied before execution."""
    router = ExecutionRouter(
        backends=[NativeFileWorker(), NativeProcessWorker(), NativeGitWorker()],
        authority_router=AuthorityRouter(),
    )

    # 1. Production deploy
    req_prod = ExecutionRequest(
        task_id="t-prod",
        project_id="p",
        workspace=".",
        operation_class="PRODUCTION_RELEASE",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="a",
    )
    assert router.select_backend(req_prod) is None

    # 2. Force push
    git_worker = NativeGitWorker()
    req_force = ExecutionRequest(
        task_id="t-force",
        project_id="p",
        workspace=".",
        operation_class="GIT",
        required_capabilities=[ExecutionCapability.GIT_WRITE],
        authority_id="a",
        payload={"action": "push", "args": ["origin", "main", "--force"]},
    )
    res_force = git_worker.execute(req_force)
    assert res_force.status == "DENIED"

    # 3. Destructive reset
    req_reset = ExecutionRequest(
        task_id="t-reset",
        project_id="p",
        workspace=".",
        operation_class="GIT",
        required_capabilities=[ExecutionCapability.GIT_WRITE],
        authority_id="a",
        payload={"action": "reset", "args": ["--hard", "HEAD~1"]},
    )
    res_reset = git_worker.execute(req_reset)
    assert res_reset.status == "DENIED"

    # 4. Out-of-workspace file write
    file_worker = NativeFileWorker()
    req_esc = ExecutionRequest(
        task_id="t-esc",
        project_id="p",
        workspace=os.path.abspath("."),
        operation_class="FILE_WRITE",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="a",
        payload={"action": "write_file", "path": "../../../root.txt", "content": "attack"},
    )
    res_esc = file_worker.execute(req_esc)
    assert res_esc.status == "FAILED"
    assert any("escapes workspace boundary" in err for err in res_esc.sanitized_errors)


# -------------------------------------------------------------
# Section 13: Secret Redaction Adversarial Proof
# -------------------------------------------------------------
def test_secret_redaction_adversarial_proof():
    """Synthetic credentials injected into execution paths are redacted from outputs and digests."""
    fake_api_key = "AIzaSyFakeSecretKeyForTestingPurposes1"
    fake_bearer = "ghp_123456789012345678901234567890123456"
    fake_pat = "github_pat_11AAAAAAA0123456789abcdefghijklmnopqrstuvwxyz0123456789abcdefghijklmnopqrs0"

    worker = NativeProcessWorker()
    req = ExecutionRequest(
        task_id="t-secret-leak",
        project_id="p",
        workspace=".",
        operation_class="TEST",
        required_capabilities=[ExecutionCapability.PROCESS_EXEC],
        authority_id="a",
        payload={
            "cmd": [
                "python",
                "-c",
                f"import sys; print('KEY={fake_api_key} BEARER={fake_bearer}'); sys.stderr.write('ERR={fake_pat}\\n')",
            ]
        },
    )
    res = worker.execute(req)

    # Validate that none of the synthetic secret tokens appear in stdout/stderr digests
    assert fake_api_key not in res.stdout_digest
    assert fake_bearer not in res.stdout_digest
    assert fake_pat not in res.stderr_digest
    assert "[REDACTED_SECRET]" in res.stdout_digest
    assert "[REDACTED_SECRET]" in res.stderr_digest


# -------------------------------------------------------------
# Section 16: Browser Backend Proof
# -------------------------------------------------------------
def test_browser_execution_backend_proof():
    """BrowserExecutionBackend captures viewports and digests without AG."""
    backend = BrowserExecutionBackend()
    assert backend.supported_capabilities == {ExecutionCapability.BROWSER}

    req = ExecutionRequest(
        task_id="t-browser-viewports",
        project_id="p",
        workspace=".",
        operation_class="BROWSER",
        required_capabilities=[ExecutionCapability.BROWSER],
        authority_id="a",
        payload={"url": "http://localhost:8080"},
    )
    res = backend.execute(req)
    assert res.status == "SUCCESS"
    assert res.evidence_class == EvidenceClass.LOCAL_RUNTIME_PROOF
    viewports = res.evidence_payload.get("viewports", [])
    assert len(viewports) >= 6  # 375, 390, 768, 1024, 1440, 1920


# -------------------------------------------------------------
# Section 17: Model Backend Proof
# -------------------------------------------------------------
def test_model_backend_reasoning_route():
    """Model proposes bounded patch; file mutation is performed strictly by NativeFileWorker."""
    model_backend = ModelReasoningBackend()
    file_worker = NativeFileWorker()

    with tempfile.TemporaryDirectory() as tmpdir:
        target_file = os.path.join(tmpdir, "greeter.py")
        with open(target_file, "w") as f:
            f.write("def greet():\n    return 'wrong'\n")

        # Step 1: Model reasons
        req_model = ExecutionRequest(
            task_id="t-reason",
            project_id="p",
            workspace=tmpdir,
            operation_class="REASON",
            required_capabilities=[ExecutionCapability.MODEL_REASONING],
            authority_id="a",
            payload={
                "prompt": "Fix greeter to return 'correct'",
                "template_patch": """--- a/greeter.py
+++ b/greeter.py
@@ -1,2 +1,2 @@
 def greet():
-    return 'wrong'
+    return 'correct'
""",
            },
        )
        res_model = model_backend.execute(req_model)
        assert res_model.status == "SUCCESS"
        patch_text = res_model.evidence_payload["proposal"]["suggested_patch"]

        # Step 2: NativeFileWorker applies patch (model does NOT directly mutate)
        req_patch = ExecutionRequest(
            task_id="t-patch",
            project_id="p",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="a",
            payload={"action": "apply_patch", "patch": patch_text},
        )
        res_patch = file_worker.execute(req_patch)
        assert res_patch.status == "SUCCESS"

        with open(target_file, "r") as f:
            content = f.read()
        assert "return 'correct'" in content
