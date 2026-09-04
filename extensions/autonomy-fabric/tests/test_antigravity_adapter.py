"""Unit tests for Antigravity Adapter (R2 / Correction R1)."""

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


def test_cli_adapter_cmd_building_without_workspace_flag():
    cli = AntigravityCLIAdapter(cli_binary_path="antigravity")
    cmd = cli.build_cmd("Hello", conversation_id="conv-789", output_format="json", continue_conversation=True)
    # Does NOT include unsupported --workspace flag
    assert "--workspace" not in cmd
    assert cmd == [
        "antigravity",
        "--output-format",
        "json",
        "--conversation",
        "conv-789",
        "--continue",
        "--prompt",
        "Hello",
    ]


def test_cli_adapter_json_envelope_parsing():
    cli = AntigravityCLIAdapter()
    stdout = json.dumps({
        "conversation_id": "conv-real-999",
        "status": "SUCCESS",
        "response": "Completed task cleanly",
        "duration_seconds": 1.25,
        "num_turns": 3,
        "usage": {"prompt_tokens": 150, "completion_tokens": 300},
    })
    resp = cli.parse_cli_json(stdout, "", 0)
    assert resp.conversation_id == "conv-real-999"
    assert resp.status == AntigravityStatus.SUCCESS
    assert resp.mapped_aos_status == RunStatus.COMPLETED
    assert resp.turn_count == 3
    assert resp.usage_metadata["prompt_tokens"] == 150


def test_cli_adapter_stream_json_envelope_parsing():
    cli = AntigravityCLIAdapter()
    stdout_stream = "\n".join([
        json.dumps({"event": "init", "conversation_id": "conv-stream-100"}),
        json.dumps({"event": "step_update", "step": 1, "action": "Analyzing workspace"}),
        json.dumps({"event": "result", "conversation_id": "conv-stream-100", "status": "SUCCESS", "num_turns": 2}),
    ])

    resp = cli.parse_cli_stream_json(stdout_stream, "", 0)
    assert resp.conversation_id == "conv-stream-100"
    assert resp.status == AntigravityStatus.SUCCESS
    assert resp.mapped_aos_status == RunStatus.COMPLETED
    assert resp.turn_count == 2


def test_unresolved_identity_fails_closed():
    cli = AntigravityCLIAdapter()
    stdout = json.dumps({"status": "ERROR", "error": "Authentication failed"})
    resp = cli.parse_cli_json(stdout, "Auth failed", 1)

    assert resp.conversation_id == "IDENTITY_UNRESOLVED"
    assert resp.status == AntigravityStatus.IDENTITY_UNRESOLVED
    assert resp.mapped_aos_status == RunStatus.FAILED
