"""Unit tests for Native Workers (NativeFileWorker, NativeProcessWorker, NativeGitWorker, etc.)."""

import pytest
import hashlib
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
    _resolve_process_argv,
)
from extensions.autonomy_fabric.exact_edit_engine import ExactEditError, apply_exact_edits


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

        # Rewriting the exact same bytes is not durable progress.
        req_noop_write = ExecutionRequest(
            task_id="t-write-noop",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="FILE_WRITE",
            required_capabilities=[ExecutionCapability.FILE_WRITE],
            authority_id="auth-1",
            payload={"action": "write_file", "path": "src/hello.py", "content": "print('hello world')\n"},
        )
        res_noop_write = worker.execute(req_noop_write)
        assert res_noop_write.status == "FAILED"
        assert any("no content change" in err for err in res_noop_write.sanitized_errors)

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


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_native_file_worker_replace_exact_is_atomic_and_preserves_unrelated_bytes(
    tmp_path: Path, monkeypatch, newline: str
):
    target = tmp_path / "components" / "MarketingLayout.tsx"
    target.parent.mkdir(parents=True)
    lines = [
        "// mobile menu",
        "React.useEffect(() => {",
        "  document.body.dataset.menu = mobileMenuOpen ? 'open' : 'closed';",
        "}, [mobileMenuOpen]);",
        "const closeMobileMenu = () => setMobileMenuOpen(false);",
        '<nav aria-label="Main navigation">',
        "  <a className=\"focus-visible:ring-2 focus-visible:ring-brand\">Home</a>",
        "</nav>",
        "",
    ]
    original = newline.join(lines).encode("utf-8")
    target.write_bytes(original)
    original_sha = hashlib.sha256(original).hexdigest()
    edits = [
        {"old": "const closeMobileMenu = () => setMobileMenuOpen(false);\n", "new": ""},
        {
            "old": 'aria-label="Main navigation"',
            "new": "aria-label={language === 'tr' ? 'Ana navigasyon' : 'Main navigation'}",
        },
    ]
    expected = apply_exact_edits(original, edits, original_sha)

    replace_calls = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    result = NativeFileWorker().execute(ExecutionRequest(
        task_id="replace-exact",
        project_id="p-test",
        workspace=str(tmp_path),
        operation_class="FILE_WRITE",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="auth-1",
        write_scope=["components/MarketingLayout.tsx"],
        payload={
            "action": "replace_exact",
            "path": "components/MarketingLayout.tsx",
            "edits": edits,
            "precondition_sha": original_sha,
            "result_sha256": expected.result_sha256,
        },
    ))

    assert result.status == "SUCCESS"
    assert target.read_bytes() == expected.result_bytes
    assert len(replace_calls) == 1
    assert replace_calls[0][0].parent == target.parent
    assert replace_calls[0][1] == target
    assert result.evidence_payload["original_sha256"] == original_sha
    assert result.evidence_payload["result_sha256"] == expected.result_sha256
    assert result.evidence_payload["newline"] == ("CRLF" if newline == "\r\n" else "LF")
    assert b"React.useEffect" in target.read_bytes()
    assert b"focus-visible:ring-2 focus-visible:ring-brand" in target.read_bytes()
    if newline == "\r\n":
        assert b"\n" not in target.read_bytes().replace(b"\r\n", b"")
    else:
        assert b"\r\n" not in target.read_bytes()


@pytest.mark.parametrize(
    ("source", "edits", "sha_override", "error_text"),
    [
        (b"alpha beta\n", [{"old": "alpha", "new": "omega"}], "0" * 64, "source SHA mismatch"),
        (b"alpha beta\n", [{"old": "missing", "new": "omega"}], None, "missing from source"),
        (b"alpha alpha\n", [{"old": "alpha", "new": "omega"}], None, "ambiguous"),
        (b"aaa", [{"old": "aa", "new": "b"}], None, "found 2 occurrences"),
        (b"aaaa", [{"old": "aa", "new": "b"}], None, "found 3 occurrences"),
        (
            b"abcdef\n",
            [{"old": "abcd", "new": "one"}, {"old": "cdef", "new": "two"}],
            None,
            "overlapping",
        ),
        (
            b"alpha beta\n",
            [{"old": "alpha", "new": "omega"}, {"old": "missing", "new": "value"}],
            None,
            "missing from source",
        ),
        (b"alpha beta\n", [{"old": "alpha", "new": "alpha"}], None, "no-op"),
    ],
    ids=[
        "wrong-sha",
        "missing",
        "ambiguous",
        "overlapping-ambiguity-two",
        "overlapping-ambiguity-three",
        "overlap",
        "atomic-multi-edit",
        "no-op",
    ],
)
def test_native_file_worker_replace_exact_validation_failure_has_zero_mutation(
    tmp_path: Path, source: bytes, edits, sha_override, error_text: str
):
    target = tmp_path / "source.txt"
    target.write_bytes(source)
    result = NativeFileWorker().execute(ExecutionRequest(
        task_id="replace-exact-invalid",
        project_id="p-test",
        workspace=str(tmp_path),
        operation_class="FILE_WRITE",
        required_capabilities=[ExecutionCapability.FILE_WRITE],
        authority_id="auth-1",
        write_scope=["source.txt"],
        payload={
            "action": "replace_exact",
            "path": "source.txt",
            "edits": edits,
            "precondition_sha": sha_override or hashlib.sha256(source).hexdigest(),
            "result_sha256": "f" * 64,
        },
    ))

    assert result.status == "FAILED"
    assert any(error_text in error for error in result.sanitized_errors)
    assert target.read_bytes() == source


def test_exact_edit_engine_rejects_mixed_newlines_and_unbounded_edits():
    mixed = b"one\r\ntwo\n"
    with pytest.raises(ExactEditError, match="mixed LF and CRLF"):
        apply_exact_edits(
            mixed,
            [{"old": "one", "new": "three"}],
            hashlib.sha256(mixed).hexdigest(),
        )

    source = b"anchor\n"
    with pytest.raises(ExactEditError, match="edit count exceeds"):
        apply_exact_edits(
            source,
            [{"old": f"missing-{index}", "new": "value"} for index in range(33)],
            hashlib.sha256(source).hexdigest(),
        )


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

        # 2b. A syntactically accepted placeholder patch must not report success
        # when it leaves the target byte-for-byte unchanged.
        placeholder_patch = """--- a/module.py
+++ b/module.py
@@
-// existing code
+// existing code
+// TODO: pending
"""
        req_placeholder = ExecutionRequest(
            task_id="t-placeholder-noop",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="auth-1",
            payload={"action": "apply_patch", "patch": placeholder_patch},
        )
        res_placeholder = worker.execute(req_placeholder)
        assert res_placeholder.status == "FAILED"
        assert any("no content change" in e for e in res_placeholder.sanitized_errors)

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



def test_windows_npx_resolution_uses_managed_node_cli_without_shell(monkeypatch, tmp_path):
    node_dir = tmp_path / "node-v24-test"
    cli_dir = node_dir / "node_modules" / "npm" / "bin"
    cli_dir.mkdir(parents=True)

    node_exe = node_dir / "node.exe"
    node_exe.write_bytes(b"MZ")

    npx_cli = cli_dir / "npx-cli.js"
    npx_cli.write_text("// synthetic npx cli\n", encoding="utf-8")

    def fake_which(name, path=None):
        if name in {"node.exe", "node"}:
            return str(node_exe)
        return None

    monkeypatch.setattr(shutil, "which", fake_which)

    argv, evidence = _resolve_process_argv(
        [
            "npx",
            "playwright",
            "test",
            "--project=chromium",
            "tests/example.spec.ts",
        ],
        {"PATH": str(node_dir)},
        platform_name="nt",
    )

    assert argv == [
        str(node_exe.resolve()),
        str(npx_cli),
        "playwright",
        "test",
        "--project=chromium",
        "tests/example.spec.ts",
    ]

    assert evidence["requested_binary"] == "npx"
    assert evidence["resolved_executable"] == str(node_exe.resolve())
    assert evidence["launcher"] == str(npx_cli)
    assert evidence["resolution_mode"] == "WINDOWS_NODE_CLI_DIRECT"


def test_windows_npx_resolution_fails_typed_when_managed_node_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, path=None: None)

    with pytest.raises(
        FileNotFoundError,
        match="PROCESS_EXECUTABLE_UNAVAILABLE",
    ):
        _resolve_process_argv(
            ["npx", "playwright", "test"],
            {"PATH": ""},
            platform_name="nt",
        )



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

        req_force_lease = ExecutionRequest(
            task_id="t-git-force-lease",
            project_id="p-test",
            workspace=tmpdir,
            operation_class="GIT",
            required_capabilities=[ExecutionCapability.GIT_WRITE],
            authority_id="auth-1",
            payload={"action": "push", "args": ["origin", "main", "--force-with-lease"]},
        )
        assert worker.execute(req_force_lease).status == "DENIED"

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
        payload={
            "url": "data:text/html,<h1>AOS Browser</h1><button class='btn-primary'>Click</button>",
            "assertions": [{"type": "selector_visible", "target": ".btn-primary"}],
        },
    )
    res_b = b_worker.execute(req_b)
    assert res_b.status == "SUCCESS"
    assert len(res_b.evidence_payload["viewports"]) == 6

    m_worker = ModelReasoningBackend(execution_mode="OFFLINE_TEST_PROVIDER")
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
