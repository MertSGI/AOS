"""Tests for AOS native browser verification and dynamic app acceptance pipeline."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from aos import runtime_worker
from aos.design_render_entry import is_authorized_loopback_url, resolve_render_entry
from aos.knowledge.hooks import ledger_for_runtime
from aos.knowledge.model import KnowledgeEventType
from aos.runtime_contract import ContinueProjectCommand, ProjectProfile
from aos.runtime_store import RuntimeStore
from extensions.autonomy_fabric.execution_backend import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionCapability,
    EvidenceClass,
)
from extensions.autonomy_fabric.native_workers import BrowserExecutionBackend
from extensions.design_intelligence.contracts import VisualEvidenceManifest


def _command(tmp_path: Path, project_id: str = "lari") -> ContinueProjectCommand:
    profile = ProjectProfile(
        project_id=project_id,
        descriptor_path=str(tmp_path / f"descriptor-{project_id}.json"),
        workspace=str(tmp_path / f"workspace-{project_id}"),
        routing_policy_path=str(tmp_path / f"policy-{project_id}.json"),
    )
    (tmp_path / f"descriptor-{project_id}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"policy-{project_id}.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"workspace-{project_id}").mkdir(exist_ok=True)
    return ContinueProjectCommand.from_mapping({"goal": "continue", "continuous": False}, project=profile)


def test_authorized_loopback_url_validation():
    """Verify authorized loopback URLs are accepted and non-loopback URLs are rejected."""
    assert is_authorized_loopback_url("http://127.0.0.1:5173/") is True
    assert is_authorized_loopback_url("http://127.0.0.1:5173/#/overview") is True
    assert is_authorized_loopback_url("http://localhost:3000/") is True
    assert is_authorized_loopback_url("http://[::1]:8080/app") is True

    # Non-loopback / remote URLs fail closed
    assert is_authorized_loopback_url("http://example.com") is False
    assert is_authorized_loopback_url("https://malicious.org/exploit") is False
    assert is_authorized_loopback_url("http://192.168.1.1:8080/") is False
    assert is_authorized_loopback_url("ftp://127.0.0.1:21/") is False
    assert is_authorized_loopback_url("file:///etc/passwd") is False


def test_browser_execution_backend_rejects_unauthorized_origin(tmp_path: Path):
    """BrowserExecutionBackend fails closed with UNAUTHORIZED_BROWSER_ORIGIN for remote URLs."""
    backend = BrowserExecutionBackend()
    req = ExecutionRequest(
        task_id="TASK-BROWSER-REMOTE",
        project_id="p",
        workspace=str(tmp_path),
        operation_class="BROWSER",
        required_capabilities=[ExecutionCapability.BROWSER],
        authority_id="a",
        payload={"url": "https://external-untrusted-site.com/login"},
    )
    res = backend.execute(req)
    assert res.status == "FAILED"
    assert "UNAUTHORIZED_BROWSER_ORIGIN" in res.sanitized_errors[0]


def test_browser_execution_backend_evaluates_assertions_pass_and_fail(tmp_path: Path):
    """BrowserExecutionBackend evaluates functional assertions and fails closed on assertion failures."""
    class MockAdapter:
        _is_test_adapter = True

        def __init__(self, assertion_results):
            self._assertion_results = assertion_results

        def capture_manifest(self, url, run_id, **kwargs):
            manifest = VisualEvidenceManifest(
                manifest_id=f"man-{run_id}",
                run_id=run_id,
                viewports_captured=[1024],
                screenshot_paths={1024: str(tmp_path / "shot.png")},
                file_hashes={1024: "0" * 64},
                horizontal_overflow_detected={1024: False},
                cta_visible={1024: True},
                capture_mode="REAL_LOCAL_BROWSER_SCREENSHOT",
                capture_adapter="RealBrowserCaptureAdapter",
            )
            manifest.console_errors = []
            manifest.page_errors = []
            manifest.dom_inspection = "PASS"
            manifest.dom_metrics = {"url": url}
            manifest.assertion_results = self._assertion_results
            return manifest

    # 1. Passing assertion
    passing_results = [{"description": "Check root", "status": "PASS", "details": "Found root element"}]
    backend_pass = BrowserExecutionBackend(capture_adapter=MockAdapter(passing_results))
    req_pass = ExecutionRequest(
        task_id="TASK-PASS",
        project_id="p",
        workspace=str(tmp_path),
        operation_class="BROWSER",
        required_capabilities=[ExecutionCapability.BROWSER],
        authority_id="a",
        payload={
            "url": "http://127.0.0.1:5173/#/test",
            "assertions": [{"type": "selector_visible", "selector": "#root"}],
        },
    )
    res_pass = backend_pass.execute(req_pass)
    assert res_pass.status == "SUCCESS"
    assert res_pass.evidence_payload["assertion_results"][0]["status"] == "PASS"

    # 2. Failing assertion
    failing_results = [{"description": "Check button", "status": "FAIL", "details": "Element button.save not found"}]
    backend_fail = BrowserExecutionBackend(capture_adapter=MockAdapter(failing_results))
    req_fail = ExecutionRequest(
        task_id="TASK-FAIL",
        project_id="p",
        workspace=str(tmp_path),
        operation_class="BROWSER",
        required_capabilities=[ExecutionCapability.BROWSER],
        authority_id="a",
        payload={
            "url": "http://127.0.0.1:5173/#/test",
            "assertions": [{"type": "selector_visible", "selector": "button.save"}],
        },
    )
    res_fail = backend_fail.execute(req_fail)
    assert res_fail.status == "FAILED"
    assert "ASSERTION_FAILED" in res_fail.sanitized_errors[0]


def test_kcp_admits_browser_verification_receipt(tmp_path: Path, monkeypatch):
    """Runtime worker admits BROWSER verified tasks into KCP verification receipts with PASS status."""
    root = tmp_path / "runtime"
    store = RuntimeStore(root)
    command = _command(tmp_path, "lari")
    store.create_command(command.to_dict())

    candidate_sha = "d" * 40
    runtime_home = root.parent

    host_rcpt = {
        "command_id": command.command_id,
        "batch_number": 1,
        "schema_version": "1.0.0",
        "timestamp": "2026-10-14T09:00:00Z",
        "project_id": "lari",
        "completed_task_ids": ["task-browser-verify"],
        "failed_task_ids": [],
        "verified_tasks": [
            {
                "task_id": "task-browser-verify",
                "run_id": "run-browser-01",
                "run_type": "BROWSER",
                "status": "PASS",
                "workspace_head_sha": candidate_sha,
                "browser_evidence": {
                    "url": "http://127.0.0.1:5173/#/dashboard",
                    "assertions": [{"name": "app-root", "status": "PASS"}],
                },
            }
        ],
        "progress": 100.0,
        "production": "NO_GO",
    }

    monkeypatch.setattr(runtime_worker, "hydrate_environment", lambda overwrite=True: {})
    monkeypatch.setattr(
        runtime_worker,
        "run_autonomous_project",
        lambda **kwargs: {
            "disposition": "BOUNDED_RUN_EXHAUSTED",
            "reason": "cycle budget reached",
            "completed_batch_count": 1,
            "completed_batches": [{"batch_number": 1, "receipt": host_rcpt}],
            "candidate_sha": candidate_sha,
        },
    )

    result = runtime_worker.execute_command(root, command.command_id)
    assert result["disposition"] == "BOUNDED_RUN_EXHAUSTED"
    assert "kcp_verification_receipt" in result["receipt"]

    saved_state = store.read_state(command.command_id)
    assert saved_state["kcp_verification_status"] == "PASS"

    ledger = ledger_for_runtime(runtime_home)
    receipts = [
        e for e in ledger.read_events()
        if e["event_type"] == KnowledgeEventType.VERIFICATION_RECEIPT.value
    ]
    assert len(receipts) == 1
    receipt_event = receipts[0]
    assert receipt_event["ci"]["disposition"] == "VERIFIED_BATCH"
    assert receipt_event["claims"].get("project_complete") is False

