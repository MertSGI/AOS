"""Unit tests for Native Workers (NativeFileWorker, NativeProcessWorker, NativeGitWorker, etc.)."""

import pytest
import os
import tempfile
import shutil
from pathlib import Path

from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionCapability,
    ExecutionHealth,
    EvidenceClass,
)
from extensions.autonomy_fabric.native_workers import (
    NativeFileWorker,
    NativeProcessWorker,
    NativeGitWorker,
    GitHubCIWorker,
    BrowserExecutionBackend,
    ModelReasoningBackend,
    AntigravityExecutionBackend,
    redact_secrets,
    compute_file_sha256,
)


def test_secret_redaction():
    text = "Authorization: Bearer ghp_123456789012345678901234567890123456 and api_key='sk_live_secretkey12345'"
    redacted = redact_secrets(text)
    assert "ghp_123456789012345678901234567890123456" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_native_file_worker_atomic_write_read_and_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeFileWorker()
        req_write = ExecutionRequest(
            task_id="t-write-1",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="FILE_WRITE",
            required_capabilities=[ExecutionCapability.FILE_WRITE],
            authority_id="auth-1",
            payload={"action": "write_file", "path": "src/hello.py", "content": "print('hello world')\n"},
        )
        res_write = worker.execute(req_write)
        assert res_write.status == "SUCCESS"
        assert "src/hello.py" in res_write.changed_paths
        created_file = os.path.join(tmpdir, "src/hello.py")
        assert os.path.exists(created_file)
        file_sha = compute_file_sha256(created_file)
        assert res_write.artifact_hashes["src/hello.py"] == file_sha

        # Read back
        req_read = ExecutionRequest(
            task_id="t-read-1",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="FILE_READ",
            required_capabilities=[ExecutionCapability.FILE_READ],
            authority_id="auth-1",
            payload={"action": "read_file", "path": "src/hello.py"},
        )
        res_read = worker.execute(req_read)
        assert res_read.status == "SUCCESS"
        assert res_read.evidence_payload["content"] == "print('hello world')\n"

        # Precondition SHA mismatch triggers rollback
        req_bad_precondition = ExecutionRequest(
            task_id="t-write-2",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="FILE_WRITE",
            required_capabilities=[ExecutionCapability.FILE_WRITE],
            authority_id="auth-1",
            payload={
                "action": "write_file",
                "path": "src/hello.py",
                "content": "corrupted",
                "precondition_sha": "0000000000000000000000000000000000000000000000000000000000000000",
            },
        )
        res_bad = worker.execute(req_bad_precondition)
        assert res_bad.status == "FAILED"
        # Content preserved
        with open(created_file, "r") as f:
            assert f.read() == "print('hello world')\n"


def test_native_file_worker_path_confinement():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeFileWorker()
        req_escape = ExecutionRequest(
            task_id="t-escape",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="FILE_WRITE",
            required_capabilities=[ExecutionCapability.FILE_WRITE],
            authority_id="auth-1",
            payload={"action": "write_file", "path": "../outside.txt", "content": "escaped"},
        )
        res = worker.execute(req_escape)
        assert res.status == "FAILED"
        assert any("escapes workspace boundary" in err for err in res.sanitized_errors)


def test_native_file_worker_unified_patch():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeFileWorker()
        file_path = os.path.join(tmpdir, "math.py")
        with open(file_path, "w") as f:
            f.write("def add(a, b):\n    return a - b\n")

        patch_text = """--- a/math.py
+++ b/math.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
        req_patch = ExecutionRequest(
            task_id="t-patch-1",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": patch_text},
        )
        res = worker.execute(req_patch)
        assert res.status == "SUCCESS"
        with open(file_path, "r") as f:
            content = f.read()
        assert "return a + b" in content


def test_native_file_worker_advanced_patch_engine():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeFileWorker()

        # 1. Multi-hunk patch with line offset
        f1 = os.path.join(tmpdir, "module.py")
        with open(f1, "w") as f:
            f.write("def f1():\n    return 1\n\n\ndef f2():\n    return 2\n")

        multi_hunk_patch = """--- a/module.py
+++ b/module.py
@@ -1,2 +1,3 @@
 def f1():
+    # docstring
     return 1
@@ -4,2 +5,3 @@
 def f2():
+    # docstring 2
     return 2
"""
        req = ExecutionRequest(
            task_id="t-multi-hunk",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": multi_hunk_patch},
        )
        res = worker.execute(req)
        assert res.status == "SUCCESS"
        with open(f1, "r") as f:
            c1 = f.read()
        assert "# docstring" in c1
        assert "# docstring 2" in c1

        # 2. Context mismatch fails closed
        bad_patch = """--- a/module.py
+++ b/module.py
@@ -1,2 +1,2 @@
 def non_existent():
-    pass
+    return True
"""
        req_bad = ExecutionRequest(
            task_id="t-bad-ctx",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": bad_patch},
        )
        res_bad = worker.execute(req_bad)
        assert res_bad.status == "FAILED"
        assert any("Context mismatch" in e for e in res_bad.sanitized_errors)

        # 3. New file creation via patch
        new_file_patch = """--- /dev/null
+++ b/created.py
@@ -0,0 +1,2 @@
+def created():
+    return True
"""
        req_new = ExecutionRequest(
            task_id="t-new-file",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": new_file_patch},
        )
        res_new = worker.execute(req_new)
        assert res_new.status == "SUCCESS"
        assert os.path.exists(os.path.join(tmpdir, "created.py"))

        # 4. Deletion denied unless explicitly authorized
        del_patch = """--- a/created.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def created():
-    return True
"""
        req_del_denied = ExecutionRequest(
            task_id="t-del-denied",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": del_patch},
        )
        res_del = worker.execute(req_del_denied)
        assert res_del.status == "FAILED"
        assert any("File deletion not authorized" in e for e in res_del.sanitized_errors)


def test_native_process_worker_execution_and_policy_denial():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeProcessWorker()
        # Allowed execution
        req_py = ExecutionRequest(
            task_id="t-py",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="TEST_EXEC",
            required_capabilities=[ExecutionCapability.PROCESS_EXEC],
            authority_id="auth-1",
            payload={"cmd": ["python", "-c", "print('process output')"]},
        )
        res_py = worker.execute(req_py)
        assert res_py.status == "SUCCESS"
        assert "process output" in res_py.stdout_digest

        # Denied unauthorized binary
        req_bad = ExecutionRequest(
            task_id="t-bad",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="TEST_EXEC",
            required_capabilities=[ExecutionCapability.PROCESS_EXEC],
            authority_id="auth-1",
            payload={"cmd": ["powershell", "-Command", "Get-Process"]},
        )
        res_bad = worker.execute(req_bad)
        assert res_bad.status == "DENIED"
        assert res_bad.exit_code == 126


def test_native_git_worker_bounded_actions_and_prohibited_protection():
    with tempfile.TemporaryDirectory() as tmpdir:
        worker = NativeGitWorker()
        # Force push prohibited
        req_force = ExecutionRequest(
            task_id="t-git-force",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="GIT",
            required_capabilities=[ExecutionCapability.GIT_WRITE],
            authority_id="auth-1",
            payload={"action": "push", "args": ["origin", "main", "--force"]},
        )
        res_force = worker.execute(req_force)
        assert res_force.status == "DENIED"

        # Prohibited destructive reset
        req_reset = ExecutionRequest(
            task_id="t-git-reset",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="GIT",
            required_capabilities=[ExecutionCapability.GIT_WRITE],
            authority_id="auth-1",
            payload={"action": "reset", "args": ["--hard", "HEAD~1"]},
        )
        res_reset = worker.execute(req_reset)
        assert res_reset.status == "DENIED"


def test_browser_and_model_backends():
    b_worker = BrowserExecutionBackend()
    assert b_worker.get_health() == ExecutionHealth.HEALTHY
    req_b = ExecutionRequest(
        task_id="t-browser",
        project_id="p-test",
        workspace=".",
        operation_class="BROWSER",
        required_capabilities=[ExecutionCapability.BROWSER],
        authority_id="auth-1",
        payload={"url": "data:text/html,<h1>AOS Browser</h1><button class='btn-primary'>Click</button>"},
    )
    res_b = b_worker.execute(req_b)
    assert res_b.status == "SUCCESS"
    assert len(res_b.evidence_payload["viewports"]) == 6

    m_worker = ModelReasoningBackend()
    assert m_worker.get_health() == ExecutionHealth.HEALTHY
    req_m = ExecutionRequest(
        task_id="t-model",
        project_id="p-test",
        workspace=".",
        operation_class="REASONING",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="auth-1",
        payload={"prompt": "Propose fix for bug"},
    )
    res_m = m_worker.execute(req_m)
    assert res_m.status == "SUCCESS"
    assert "proposal" in res_m.evidence_payload
