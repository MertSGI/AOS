"""Unit tests for Antigravity Adapter (R2 / Final Correction)."""

import pytest
import json
import tempfile
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
    assert "--workspace" not in cmd
    assert cmd == [
        cli.cli_binary_path,
        "--output-format",
        "json",
        "--conversation",
        "conv-789",
        "--continue",
        "--prompt",
        "Hello",
    ]


def test_cli_adapter_json_envelope_parsing_success():
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


def test_json_missing_conversation_id_fails_closed():
    cli = AntigravityCLIAdapter()

    # 1. returncode=0 + missing conversation_id
    stdout_code0 = json.dumps({"status": "SUCCESS", "response": "No ID here"})
    resp0 = cli.parse_cli_json(stdout_code0, "", 0)
    assert resp0.conversation_id is None
    assert resp0.status == AntigravityStatus.IDENTITY_UNRESOLVED
    assert resp0.mapped_aos_status == RunStatus.FAILED

    # 2. returncode!=0 + missing conversation_id
    stdout_code1 = json.dumps({"status": "ERROR", "error": "Internal failure"})
    resp1 = cli.parse_cli_json(stdout_code1, "Error message", 1)
    assert resp1.conversation_id is None
    assert resp1.status == AntigravityStatus.IDENTITY_UNRESOLVED
    assert resp1.mapped_aos_status == RunStatus.FAILED


def test_stream_json_terminal_contract():
    cli = AntigravityCLIAdapter()

    # Case 1: init -> result
    stream_init_result = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c-1"}),
        json.dumps({"event": "result", "conversation_id": "c-1", "status": "SUCCESS"}),
    ])
    resp1 = cli.parse_cli_stream_json(stream_init_result, "", 0)
    assert resp1.conversation_id == "c-1"
    assert resp1.status == AntigravityStatus.SUCCESS
    assert resp1.mapped_aos_status == RunStatus.COMPLETED

    # Case 2: init -> step_update -> result
    stream_full = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c-2"}),
        json.dumps({"event": "step_update", "step": 1}),
        json.dumps({"event": "result", "conversation_id": "c-2", "status": "SUCCESS"}),
    ])
    resp2 = cli.parse_cli_stream_json(stream_full, "", 0)
    assert resp2.conversation_id == "c-2"
    assert resp2.status == AntigravityStatus.SUCCESS

    # Case 3: init only (missing terminal result)
    stream_init_only = json.dumps({"event": "init", "conversation_id": "c-3"})
    resp3 = cli.parse_cli_stream_json(stream_init_only, "", 0)
    assert resp3.status == AntigravityStatus.INVALID
    assert resp3.mapped_aos_status == RunStatus.FAILED

    # Case 4: init -> step_update without result
    stream_no_result = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c-4"}),
        json.dumps({"event": "step_update", "step": 1}),
    ])
    resp4 = cli.parse_cli_stream_json(stream_no_result, "", 0)
    assert resp4.status == AntigravityStatus.INVALID
    assert resp4.mapped_aos_status == RunStatus.FAILED

    # Case 5: malformed terminal event
    stream_malformed = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c-5"}),
        json.dumps({"event": "result"}),  # missing status
    ])
    resp5 = cli.parse_cli_stream_json(stream_malformed, "", 0)
    assert resp5.status == AntigravityStatus.UNKNOWN
    assert resp5.mapped_aos_status == RunStatus.FAILED

    # Case 6: unknown terminal status
    stream_unknown_status = "\n".join([
        json.dumps({"event": "init", "conversation_id": "c-6"}),
        json.dumps({"event": "result", "conversation_id": "c-6", "status": "SOMETHING_NEW_ENUM"}),
    ])
    resp6 = cli.parse_cli_stream_json(stream_unknown_status, "", 0)
    assert resp6.status == AntigravityStatus.UNKNOWN
    assert resp6.mapped_aos_status == RunStatus.FAILED


def test_nonexistent_workspace_path_fails_immediately():
    cli = AntigravityCLIAdapter(cli_binary_path="antigravity")
    fake_path = "/nonexistent/directory/path/for/workspace/test"

    with pytest.raises(ValueError, match="does not exist or is not a directory"):
        cli.execute_prompt("Test prompt", workspace_path=fake_path)


def test_portable_discovery_explicit_path_wins():
    path = AntigravityCLIAdapter.discover_cli_binary("/custom/explicit/path/antigravity")
    assert path == "/custom/explicit/path/antigravity"


def test_portable_discovery_env_override_wins(monkeypatch):
    monkeypatch.setenv("AOS_ANTIGRAVITY_CLI_PATH", "/env/override/antigravity")
    path = AntigravityCLIAdapter.discover_cli_binary()
    assert path == "/env/override/antigravity"


def test_portable_discovery_path_which_wins(monkeypatch):
    import shutil
    monkeypatch.delenv("AOS_ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/antigravity" if cmd == "antigravity" else None)
    path = AntigravityCLIAdapter.discover_cli_binary()
    assert path == "antigravity"


def test_portable_discovery_localappdata_single_candidate(monkeypatch):
    import os
    import shutil
    monkeypatch.delenv("AOS_ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LOCALAPPDATA", tmp_dir)
        bin_dir = os.path.join(tmp_dir, "AOS", "runtime", "antigravity-cli", "1.0.0")
        os.makedirs(bin_dir)
        bin_file = os.path.join(bin_dir, "antigravity.exe")
        open(bin_file, "w").close()

        discovered = AntigravityCLIAdapter.discover_cli_binary()
        assert discovered == bin_file


def test_portable_discovery_missing_localappdata_fails_closed(monkeypatch):
    import shutil
    monkeypatch.delenv("AOS_ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(ValueError, match="CLI_BINARY_NOT_FOUND"):
        AntigravityCLIAdapter.discover_cli_binary()


def test_portable_discovery_multiple_candidates_ambiguous_fails_closed(monkeypatch):
    import os
    import shutil
    monkeypatch.delenv("AOS_ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("LOCALAPPDATA", tmp_dir)
        bin_dir1 = os.path.join(tmp_dir, "AOS", "runtime", "antigravity-cli", "1.0.0")
        bin_dir2 = os.path.join(tmp_dir, "AOS", "runtime", "antigravity-cli", "1.1.0")
        os.makedirs(bin_dir1)
        os.makedirs(bin_dir2)
        open(os.path.join(bin_dir1, "antigravity.exe"), "w").close()
        open(os.path.join(bin_dir2, "antigravity.exe"), "w").close()

        with pytest.raises(ValueError, match="CLI_BINARY_AMBIGUOUS"):
            AntigravityCLIAdapter.discover_cli_binary()


def test_no_username_specific_path_in_source():
    import os
    adapter_file = os.path.join(os.path.dirname(__file__), "..", "antigravity_adapter.py")
    content = open(adapter_file, "r", encoding="utf-8").read()
    assert "mozcelikbas" not in content
    assert "C:\\Users\\" not in content

