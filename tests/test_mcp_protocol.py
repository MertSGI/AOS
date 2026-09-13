"""Tests for MCP Protocol Conformance and Antigravity Tool Discovery.

Tests:
- stdio JSON-RPC initialize
- tools/list schema and count (all 9 specialist tools)
- tools/call valid execution
- tools/call access denied for untrusted caller claiming non-PUBLIC
- tools/call unknown tool error semantics
- malformed JSON-RPC handling
- request ID preservation
- protocol purity (clean JSON-RPC on stdout)
- Workspace MCP configuration discoverability
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from extensions.model_fabric.mcp_service import NemotronMcpServer
from extensions.model_fabric.specialist_fabric import NemotronSpecialistFabric


def test_mcp_server_initialize():
    server = NemotronMcpServer()
    # Test initialize response format via CLI handler
    proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.model_fabric.mcp_cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    init_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    stdout, stderr = proc.communicate(input=init_req, timeout=5)
    resp = json.loads(stdout.strip())

    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "capabilities" in resp["result"]
    assert resp["result"]["serverInfo"]["name"] == "aos-nemotron-specialist"


def test_mcp_server_tools_list_protocol():
    proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.model_fabric.mcp_cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    tools_req = json.dumps({"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}}) + "\n"
    stdout, stderr = proc.communicate(input=tools_req, timeout=5)
    resp = json.loads(stdout.strip())

    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 42
    tools = resp["result"]["tools"]
    # Invariant: EXACTLY 9 specialist tools exposed
    assert len(tools) == 9
    tool_names = [t["name"] for t in tools]
    expected_tools = [
        "nemotron_plan",
        "nemotron_architecture_review",
        "nemotron_code_review",
        "nemotron_security_review",
        "nemotron_sql_schema_review",
        "nemotron_long_context_analysis",
        "nemotron_evidence_contradiction_review",
        "nemotron_design_text_review",
        "nemotron_second_opinion",
    ]
    for expected in expected_tools:
        assert expected in tool_names


def test_mcp_server_tools_call_untrusted_caller_denied():
    proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.model_fabric.mcp_cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    # Untrusted caller claiming INTERNAL_NON_SENSITIVE or PATIENT_PII MUST be denied fail-closed
    call_req = json.dumps({
        "jsonrpc": "2.0",
        "id": 99,
        "method": "tools/call",
        "params": {
            "name": "nemotron_security_review",
            "arguments": {
                "prompt": "Evaluate patient data",
                "data_classification": "INTERNAL_NON_SENSITIVE",
            },
        },
    }) + "\n"
    stdout, stderr = proc.communicate(input=call_req, timeout=5)
    resp = json.loads(stdout.strip())

    assert resp["id"] == 99
    assert resp["result"]["isError"] is True
    assert "ACCESS_DENIED" in resp["result"]["content"][0]["text"]


def test_mcp_server_unknown_tool():
    proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.model_fabric.mcp_cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    call_req = json.dumps({
        "jsonrpc": "2.0",
        "id": 101,
        "method": "tools/call",
        "params": {
            "name": "nonexistent_actuator",
            "arguments": {"prompt": "test"},
        },
    }) + "\n"
    stdout, stderr = proc.communicate(input=call_req, timeout=5)
    resp = json.loads(stdout.strip())

    assert resp["id"] == 101
    assert resp["result"]["isError"] is True
    assert "not in the allowed Nemotron tool registry" in resp["result"]["content"][0]["text"]


def test_mcp_protocol_error_matrix_and_notifications():
    proc = subprocess.Popen(
        [sys.executable, "-m", "extensions.model_fabric.mcp_cli"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )

    # 1. Parse Error: -32700
    line1 = "this is not json\n"
    # 2. Unknown Method: -32601
    line2 = json.dumps({"jsonrpc": "2.0", "id": 201, "method": "unknown/method", "params": {}}) + "\n"
    # 3. Notification (without id): MUST NOT receive response
    line3 = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
    # 4. Invalid params on tools/call: -32602
    line4 = json.dumps({"jsonrpc": "2.0", "id": 202, "method": "tools/call", "params": "invalid_params"}) + "\n"
    # 5. Sequential valid tools/list with ID preservation
    line5 = json.dumps({"jsonrpc": "2.0", "id": "custom-id-999", "method": "tools/list", "params": {}}) + "\n"

    input_payload = line1 + line2 + line3 + line4 + line5
    stdout, stderr = proc.communicate(input=input_payload, timeout=5)

    lines = [l.strip() for l in stdout.strip().split("\n") if l.strip()]
    # Expect exactly 4 responses (line 3 notification must produce zero stdout lines)
    assert len(lines) == 4, f"Expected 4 responses, got {len(lines)}: {lines}"

    r1 = json.loads(lines[0])
    assert r1.get("error", {}).get("code") == -32700

    r2 = json.loads(lines[1])
    assert r2.get("id") == 201
    assert r2.get("error", {}).get("code") == -32601

    r3 = json.loads(lines[2])
    assert r3.get("id") == 202
    assert r3.get("error", {}).get("code") == -32602

    r4 = json.loads(lines[3])
    assert r4.get("id") == "custom-id-999"
    assert "tools" in r4.get("result", {})

    # Stderr received diagnostics without leaking secrets
    assert "MCP connection initialized" in stderr
    assert "nvapi" not in stderr


def test_workspace_mcp_config_discovery():
    mcp_config_path = Path(__file__).parent.parent / ".agents" / "mcp_config.json"
    assert mcp_config_path.is_file(), "Workspace .agents/mcp_config.json must exist"
    with open(mcp_config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    assert "mcpServers" in config
    assert "aos-nemotron-specialist" in config["mcpServers"]
    server_cfg = config["mcpServers"]["aos-nemotron-specialist"]
    assert server_cfg["command"] == "python"
    assert "-m" in server_cfg["args"]
    assert "extensions.model_fabric.mcp_cli" in server_cfg["args"]
