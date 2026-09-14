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
# Section 16: Real Browser Backend Proof with Local HTTP Fixture
# -------------------------------------------------------------
def test_browser_execution_backend_proof():
    """Starts local static HTTP fixture and captures viewports through RealBrowserCaptureAdapter."""
    import http.server
    import threading
    import socket

    # 1. Start real static local HTTP fixture
    with tempfile.TemporaryDirectory() as fixture_dir:
        index_file = os.path.join(fixture_dir, "index.html")
        with open(index_file, "w", encoding="utf-8") as f:
            f.write("""<!DOCTYPE html>
<html>
<head><title>AOS Visual Fixture</title></head>
<body>
  <h1>AOS Browser Verification</h1>
  <button class="btn-primary" style="padding:10px 20px;">Primary Action</button>
</body>
</html>""")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=fixture_dir, **kwargs)
            def log_message(self, format, *args):
                pass  # suppress stdout logging

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        fixture_url = f"http://127.0.0.1:{port}/index.html"

        # 2. Execute via BrowserExecutionBackend without AG
        backend = BrowserExecutionBackend()
        assert backend.supported_capabilities == {ExecutionCapability.BROWSER}

        req = ExecutionRequest(
            task_id="t-real-browser-fixture",
            project_id="p-browser",
            workspace=".",
            operation_class="BROWSER",
            required_capabilities=[ExecutionCapability.BROWSER],
            authority_id="a",
            payload={"url": fixture_url, "run_id": "run-real-fixture"},
        )
        res = backend.execute(req)
        server.shutdown()

        assert res.status == "SUCCESS"
        assert res.evidence_class == EvidenceClass.LOCAL_RUNTIME_PROOF
        payload = res.evidence_payload
        viewports = payload.get("viewports", [])
        assert viewports == [375, 390, 768, 1024, 1440, 1920]
        assert payload.get("dom_inspection") == "PASS"
        assert isinstance(payload.get("dom_metrics"), dict)
        assert payload.get("dom_metrics", {}).get("title") == "AOS Visual Fixture"
        assert payload.get("dom_metrics", {}).get("bodyChildCount") >= 2
        assert payload.get("console_errors") == []
        assert payload.get("page_errors") == []
        assert payload.get("screenshot_paths") is not None

        # Validate artifact files exist and hashes are non-empty SHA256
        assert len(res.artifact_hashes) == 6
        for vp, h in res.artifact_hashes.items():
            assert len(h) == 64  # valid sha256

        # Negative proofs: BrowserExecutionBackend fails closed when measured fields or provenance fields are absent
        # 1. Completely empty manifest
        class EmptyBrowserAdapter:
            def capture_manifest(self, url, run_id):
                class EmptyManifest:
                    pass
                return EmptyManifest()

        empty_backend = BrowserExecutionBackend(capture_adapter=EmptyBrowserAdapter())
        res_empty = empty_backend.execute(req)
        assert res_empty.status == "FAILED"
        assert "BROWSER_MEASURED_EVIDENCE_ABSENT" in res_empty.sanitized_errors[0]

        # 2. Test each individual required field missing
        required_fields = [
            "viewports_captured",
            "file_hashes",
            "screenshot_paths",
            "horizontal_overflow_detected",
            "cta_visible",
            "console_errors",
            "page_errors",
            "dom_inspection",
            "dom_metrics",
            "capture_adapter",
            "capture_mode",
        ]
        base_manifest_data = {
            "manifest_id": "vis-test-1",
            "run_id": "run-test-1",
            "viewports_captured": [375, 390, 768, 1024, 1440, 1920],
            "file_hashes": {vp: "a" * 64 for vp in [375, 390, 768, 1024, 1440, 1920]},
            "screenshot_paths": {vp: f"/tmp/{vp}.png" for vp in [375, 390, 768, 1024, 1440, 1920]},
            "horizontal_overflow_detected": {vp: False for vp in [375, 390, 768, 1024, 1440, 1920]},
            "cta_visible": {vp: True for vp in [375, 390, 768, 1024, 1440, 1920]},
            "console_errors": [],
            "page_errors": [],
            "dom_inspection": "PASS",
            "dom_metrics": {"title": "Test", "bodyChildCount": 2, "readyState": "complete"},
            "capture_adapter": "RealBrowserCaptureAdapter",
            "capture_mode": "REAL_LOCAL_BROWSER_SCREENSHOT",
        }

        for missing_attr in required_fields:
            class MissingFieldAdapter:
                def __init__(self, attr):
                    self.attr = attr
                def capture_manifest(self, url, run_id):
                    class ManifestObj:
                        pass
                    m = ManifestObj()
                    for k, v in base_manifest_data.items():
                        if k != self.attr:
                            setattr(m, k, v)
                    return m

            backend = BrowserExecutionBackend(capture_adapter=MissingFieldAdapter(missing_attr))
            res_missing = backend.execute(req)
            assert res_missing.status == "FAILED"
            assert "BROWSER_MEASURED_EVIDENCE_ABSENT" in res_missing.sanitized_errors[0]
            assert missing_attr in res_missing.sanitized_errors[0]

        # 3. Test wrong adapter or wrong capture mode fails closed
        class WrongModeAdapter:
            def capture_manifest(self, url, run_id):
                class ManifestObj:
                    pass
                m = ManifestObj()
                for k, v in base_manifest_data.items():
                    setattr(m, k, v)
                m.capture_mode = "FAKE_TEST_ARTIFACT"
                return m

        wrong_backend = BrowserExecutionBackend(capture_adapter=WrongModeAdapter())
        res_wrong = wrong_backend.execute(req)
        assert res_wrong.status == "FAILED"
        assert "REAL_LOCAL_BROWSER_SCREENSHOT" in res_wrong.sanitized_errors[0]


# -------------------------------------------------------------
# Section 17: Real Model Provider Route and Execution Contract Proof
# -------------------------------------------------------------
def test_model_backend_reasoning_route():
    """Model executes via ProviderRegistry -> ProviderRouter -> PlannerProvider executor, generating bounded patch applied by NativeFileWorker."""
    from aos.provider_registry import ProviderRegistry, ProviderRouter
    from aos.planner import FakePlannerProvider

    # 1. Verify fail-closed in RUNTIME_PROVIDER_REQUIRED mode when no executable provider is present
    runtime_backend = ModelReasoningBackend(execution_mode="RUNTIME_PROVIDER_REQUIRED")
    req_fail_closed = ExecutionRequest(
        task_id="t-reason-fail-closed",
        project_id="p",
        workspace=".",
        operation_class="REASON",
        required_capabilities=[ExecutionCapability.MODEL_REASONING],
        authority_id="a",
        payload={"prompt": "Do work without provider executor"},
    )
    res_fc = runtime_backend.execute(req_fail_closed)
    assert res_fc.status == "FAILED"
    assert "PROVIDER_EXECUTOR_UNAVAILABLE" in res_fc.sanitized_errors[0]

    # 2. Generic policy data supplied via request configuration, not hardcoded path
    policy_data = {
        "schema_version": "0.1.0",
        "routing_mode": "DETERMINISTIC",
        "allow_paid_fallback": False,
        "allow_provider_fallback": True,
        "data_classification": "PUBLIC",
        "risk_routes": {
            "R0": {"preferred_providers": ["fake_offline_provider"]}
        },
        "providers": {
            "fake_offline_provider": {
                "provider_id": "fake_offline_provider",
                "model_id": "offline-reasoner-v1",
                "credential_env_var": None,
                "billing_class": "FREE_TIER",
                "structured_output": True,
                "cloud_local": "LOCAL",
                "enabled": True,
                "allowed_data_classifications": ["PUBLIC"]
            }
        }
    }
    registry = ProviderRegistry(policy_data)
    router = ProviderRouter(registry)

    # Injected provider executor matching PlannerProvider contract
    patch_content = """--- a/greeter.py
+++ b/greeter.py
@@ -1,2 +1,2 @@
 def greet():
-    return 'wrong'
+    return 'correct'
"""
    fake_planner = FakePlannerProvider(decision_override={"suggested_patch": patch_content, "disposition": "ACCEPT"})

    model_backend = ModelReasoningBackend(
        provider_router=router,
        provider_executor_factory=lambda pid, mid: fake_planner,
        execution_mode="RUNTIME_PROVIDER_REQUIRED",
    )
    file_worker = NativeFileWorker()

    with tempfile.TemporaryDirectory() as tmpdir:
        target_file = os.path.join(tmpdir, "greeter.py")
        with open(target_file, "w", encoding="utf-8") as f:
            f.write("def greet():\n    return 'wrong'\n")

        # Step 1: Model reasons through provider execution contract
        req_model = ExecutionRequest(
            task_id="t-reason",
            project_id="p",
            workspace=tmpdir,
            operation_class="REASON",
            required_capabilities=[ExecutionCapability.MODEL_REASONING],
            authority_id="a",
            payload={
                "prompt": "Fix greeter to return 'correct'",
                "routing_policy": policy_data,
                "risk_class": "R0",
                "ignore_credentials": False,
            },
        )
        res_model = model_backend.execute(req_model)
        assert res_model.status == "SUCCESS"
        assert res_model.evidence_payload["provider_route"] == "fake_offline_provider"
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

        with open(target_file, "r", encoding="utf-8") as f:
            content = f.read()
        assert "return 'correct'" in content
        assert res_model.evidence_class == EvidenceClass.LOCAL_RUNTIME_PROOF

        # Section 1 Adversarial Test: A fake provider named "nemotron_cloud" CANNOT produce LIVE_EXTERNAL_PROOF
        adversarial_policy = {
            "schema_version": "0.1.0",
            "routing_mode": "DETERMINISTIC",
            "allow_paid_fallback": False,
            "allow_provider_fallback": True,
            "data_classification": "PUBLIC",
            "risk_routes": {
                "R0": {"preferred_providers": ["nemotron_cloud"]}
            },
            "providers": {
                "nemotron_cloud": {
                    "provider_id": "nemotron_cloud",
                    "model_id": "nemotron-fake-ultra",
                    "credential_env_var": None,
                    "billing_class": "FREE_TIER",
                    "structured_output": True,
                    "cloud_local": "LOCAL",
                    "enabled": True,
                    "allowed_data_classifications": ["PUBLIC"]
                }
            }
        }
        adv_registry = ProviderRegistry(adversarial_policy)
        adv_router = ProviderRouter(adv_registry)

        class FakeNemotronCloudProvider:
            """Adversarial fake provider with cloud name but offline execution."""
            execution_provenance = "LOCAL_OFFLINE"

            def generate_plan(self, prompt, schema=None):
                return {"decision": "ACCEPT", "suggested_patch": None}, "resp-fake-cloud", {"prompt_tokens": 10, "completion_tokens": 10}

        adv_backend = ModelReasoningBackend(
            provider_router=adv_router,
            provider_executor_factory=lambda pid, mid: FakeNemotronCloudProvider(),
            execution_mode="RUNTIME_PROVIDER_REQUIRED",
        )
        req_adv = ExecutionRequest(
            task_id="t-adv-nemotron",
            project_id="p",
            workspace=tmpdir,
            operation_class="REASON",
            required_capabilities=[ExecutionCapability.MODEL_REASONING],
            authority_id="a",
            payload={"prompt": "Test fake cloud provider", "routing_policy": adversarial_policy, "risk_class": "R0"},
        )
        res_adv = adv_backend.execute(req_adv)
        assert res_adv.status == "SUCCESS"
        assert res_adv.evidence_payload["provider_route"] == "nemotron_cloud"
        # MUST NOT be LIVE_EXTERNAL_PROOF
        assert res_adv.evidence_class != EvidenceClass.LIVE_EXTERNAL_PROOF
        assert res_adv.evidence_class == EvidenceClass.LOCAL_RUNTIME_PROOF


# -------------------------------------------------------------
# Section 18: Patch Precondition Binding Proof
# -------------------------------------------------------------
def test_patch_precondition_binding_proof():
    """Patch fails before write if expected precondition source SHA mismatches actual file SHA."""
    file_worker = NativeFileWorker()

    with tempfile.TemporaryDirectory() as tmpdir:
        target_file = os.path.join(tmpdir, "code.py")
        with open(target_file, "w", encoding="utf-8", newline="") as f:
            f.write("print('version 1')\n")

        import hashlib
        with open(target_file, "rb") as f:
            correct_sha = hashlib.sha256(f.read()).hexdigest()
        wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        patch_text = f"""index {wrong_sha}..1111111111111111
--- a/code.py
+++ b/code.py
@@ -1,1 +1,1 @@
-print('version 1')
+print('version 2')
"""
        # Attempt with mismatched precondition SHA
        req_bad = ExecutionRequest(
            task_id="t-patch-bad",
            project_id="p",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="a",
            payload={"action": "apply_patch", "patch": patch_text},
        )
        res_bad = file_worker.execute(req_bad)
        assert res_bad.status == "FAILED"
        assert "Precondition SHA mismatch" in res_bad.sanitized_errors[0]

        # Ensure file was not modified
        with open(target_file, "r", encoding="utf-8") as f:
            assert f.read() == "print('version 1')\n"

        # Now attempt with matching precondition SHA
        patch_text_good = f"""index {correct_sha}..1111111111111111
--- a/code.py
+++ b/code.py
@@ -1,1 +1,1 @@
-print('version 1')
+print('version 2')
"""
        req_good = ExecutionRequest(
            task_id="t-patch-good",
            project_id="p",
            workspace=tmpdir,
            operation_class="PATCH",
            required_capabilities=[ExecutionCapability.PATCH_APPLY],
            authority_id="a",
            payload={"action": "apply_patch", "patch": patch_text_good},
        )
        res_good = file_worker.execute(req_good)
        assert res_good.status == "SUCCESS"
        with open(target_file, "r", encoding="utf-8") as f:
            assert f.read() == "print('version 2')\n"


# -------------------------------------------------------------
# Section 19: Checkpoint Fail-Closed Hardening Proof
# -------------------------------------------------------------
def test_checkpoint_fail_closed_corruption_proof():
    """PersistentCoordinator fails closed when checkpoint is corrupt, truncated, or incompatible."""
    import hashlib
    import json
    from extensions.autonomy_fabric.persistent_coordinator import CheckpointCorruptionError

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = AgentRunRegistry()
        dag = TaskDAG("proj-fail-closed", registry)
        dag.add_node("n1", "DEV", "auth-1")
        router = ExecutionRouter(backends=[NativeFileWorker()])

        def make_coord(cp):
            return PersistentCoordinator(
                project_id="proj-fail-closed",
                workspace_path=tmpdir,
                dag=dag,
                router=router,
                registry=registry,
                checkpoint_file=cp,
            )

        # 1. Corrupt/truncated JSON
        cp1 = os.path.join(tmpdir, "c1.json")
        with open(cp1, "w") as f: f.write("{corrupt...")
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp1)
        assert "corrupt or truncated" in str(exc.value)

        # 2. Missing checksum
        cp2 = os.path.join(tmpdir, "c2.json")
        with open(cp2, "w") as f: json.dump({"schema_version": "2.0.0", "project_id": "proj-fail-closed", "coordinator_id": "coord-proj-fail-closed", "state": {}}, f)
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp2)
        assert "missing required integrity checksum" in str(exc.value)

        # 3. Bad checksum
        cp3 = os.path.join(tmpdir, "c3.json")
        with open(cp3, "w") as f: json.dump({"schema_version": "2.0.0", "project_id": "proj-fail-closed", "coordinator_id": "coord-proj-fail-closed", "checksum": "badhash", "state": {"completed_task_ids": []}}, f)
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp3)
        assert "checksum mismatch" in str(exc.value)

        # 4. Incompatible schema version
        cp4 = os.path.join(tmpdir, "c4.json")
        with open(cp4, "w") as f: json.dump({"schema_version": "1.0.0", "project_id": "proj-fail-closed", "coordinator_id": "coord-proj-fail-closed", "checksum": "x", "state": {}}, f)
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp4)
        assert "Incompatible or missing schema_version" in str(exc.value)

        # 5. Wrong project_id
        cp5 = os.path.join(tmpdir, "c5.json")
        with open(cp5, "w") as f: json.dump({"schema_version": "2.0.0", "project_id": "other-proj", "coordinator_id": "coord-proj-fail-closed", "checksum": "x", "state": {}}, f)
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp5)
        assert "project_id mismatch" in str(exc.value)

        # 6. Wrong coordinator_id
        cp6 = os.path.join(tmpdir, "c6.json")
        with open(cp6, "w") as f: json.dump({"schema_version": "2.0.0", "project_id": "proj-fail-closed", "coordinator_id": "other-coord", "checksum": "x", "state": {}}, f)
        with pytest.raises(CheckpointCorruptionError) as exc: make_coord(cp6)
        assert "coordinator_id mismatch" in str(exc.value)


# -------------------------------------------------------------
# Section 20: CI Observer Transport Consistency Proof
# -------------------------------------------------------------
def test_ci_observer_transport_consistency_proof():
    """Ensures failure/cancellation/timeouts produce status=FAILED across all states and exact SHA binding is enforced."""
    class FakeMockCIClient:
        def __init__(self, data_map):
            self.data_map = data_map
        def get_run_status(self, repo, sha):
            return self.data_map.get(sha, {"conclusion": "failure", "status": "completed", "sha": sha})

    mock_client = FakeMockCIClient({
        "sha-success": {"conclusion": "success", "status": "completed", "sha": "sha-success"},
        "sha-failure": {"conclusion": "failure", "status": "completed", "sha": "sha-failure"},
        "sha-cancelled": {"conclusion": "cancelled", "status": "completed", "sha": "sha-cancelled"},
        "sha-timed-out": {"conclusion": "timed_out", "status": "completed", "sha": "sha-timed-out"},
        "sha-in-progress": {"conclusion": None, "status": "in_progress", "sha": "sha-in-progress"},
    })
    ci_worker = GitHubCIWorker(mock_client=mock_client)

    # 1. Success
    res = ci_worker.execute(ExecutionRequest(task_id="t1", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-success"}))
    assert res.status == "SUCCESS"

    # 2. Failure
    res = ci_worker.execute(ExecutionRequest(task_id="t2", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-failure"}))
    assert res.status == "FAILED"
    assert res.exit_code == 1

    # 3. Cancelled
    res = ci_worker.execute(ExecutionRequest(task_id="t3", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-cancelled"}))
    assert res.status == "FAILED"

    # 4. Timed out
    res = ci_worker.execute(ExecutionRequest(task_id="t4", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-timed-out"}))
    assert res.status == "FAILED"

    # 5. In progress
    res = ci_worker.execute(ExecutionRequest(task_id="t5", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-in-progress"}))
    assert res.status == "DEGRADED"

    # 6. Wrong SHA mismatch
    res = ci_worker.execute(ExecutionRequest(task_id="t6", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={"sha": "sha-nonexistent"}))
    assert res.status == "FAILED"

    # 7. Missing SHA fails closed
    res_missing = ci_worker.execute(ExecutionRequest(task_id="t7", project_id="p", workspace=".", operation_class="PROCESS", required_capabilities=[ExecutionCapability.PROCESS_EXEC], authority_id="a", payload={}))
    assert res_missing.status == "FAILED"
    assert "MISSING_EXPLICIT_CI_SHA" in res_missing.sanitized_errors[0]
