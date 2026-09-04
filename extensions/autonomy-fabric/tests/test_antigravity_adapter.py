"""Unit tests for Antigravity Adapter (R2)."""

import pytest
import json
from extensions.autonomy_fabric.antigravity_adapter import (
    FakeAntigravityAdapter,
    AntigravityCLIAdapter,
    AntigravityStatus,
    ANTIGRAVITY_TO_AOS_STATUS_MAP,
)
from extensions.autonomy_fabric.run_registry import RunStatus


def test_fake_adapter_deterministic_execution():
    adapter = FakeAntigravityAdapter()
    resp = adapter.execute_prompt("Analyze workspace architecture", conversation_id="conv-123")

    assert resp.conversation_id == "conv-123"
    assert resp.status == AntigravityStatus.SUCCESS
    assert resp.mapped_aos_status == RunStatus.COMPLETED
    assert len(adapter.invocations) == 1
    assert adapter.invocations[0]["prompt"] == "Analyze workspace architecture"


def test_fake_adapter_canned_response():
    adapter = FakeAntigravityAdapter()
    canned = adapter.execute_prompt("Setup", conversation_id="conv-custom")
    canned.status = AntigravityStatus.WAITING
    canned.mapped_aos_status = RunStatus.WAITING_AGENT
    adapter.set_canned_response("conv-custom", canned)

    resp = adapter.execute_prompt("Setup", conversation_id="conv-custom")
    assert resp.status == AntigravityStatus.WAITING
    assert resp.mapped_aos_status == RunStatus.WAITING_AGENT


def test_cli_adapter_cmd_building():
    cli = AntigravityCLIAdapter(cli_binary_path="antigravity")
    cmd = cli.build_cmd("Hello", conversation_id="conv-789", workspace_path="/tmp/ws")
    assert cmd == [
        "antigravity",
        "--output-format",
        "json",
        "--conversation",
        "conv-789",
        "--workspace",
        "/tmp/ws",
        "--prompt",
        "Hello",
    ]


def test_status_mapping_fail_closed():
    cli = AntigravityCLIAdapter()
    stdout = json.dumps({"conversation_id": "c1", "status": "INVALID"})
    resp = cli.parse_cli_output(stdout, "", 0)
    assert resp.status == AntigravityStatus.INVALID
    assert resp.mapped_aos_status == RunStatus.FAILED

    # Unknown status fails closed to FAILED
    stdout_unknown = json.dumps({"conversation_id": "c2", "status": "SOMETHING_NEW"})
    resp_unknown = cli.parse_cli_output(stdout_unknown, "", 0)
    assert resp_unknown.status == AntigravityStatus.UNKNOWN
    assert resp_unknown.mapped_aos_status == RunStatus.FAILED
