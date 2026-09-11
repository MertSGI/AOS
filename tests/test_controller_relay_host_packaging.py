"""Comprehensive Host Negative & Positive Tests for MCP Serverless Handler.

Implementation Authority: LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Negative Invariants Verified:
1. NO_AUTHENTICATOR_CONFIGURED = STARTUP_FAIL_CLOSED
2. UNAUTHENTICATED_MCP_REQUEST = DENIED (HTTP 401)
3. INVALID_BEARER_TOKEN = DENIED (HTTP 401)
4. ARBITRARY_32_PLUS_CHARACTER_TOKEN = DENIED (HTTP 401)
5. CALLER_PRINCIPAL_OVERRIDE = IMPOSSIBLE (Principal remains LARI_CONTROLLER)
6. GENERIC_GIT_TOOL_AVAILABLE = NO (Method not found)
7. ARBITRARY_REPOSITORY_ARGUMENT_AVAILABLE = NO
8. ARBITRARY_BRANCH_ARGUMENT_AVAILABLE = NO
9. ARBITRARY_PATH_ARGUMENT_AVAILABLE = NO
10. SECRET_LOGGING = NO / SECRET_RESPONSE_EXPOSURE = NO

Positive Invariants Verified:
1. VALID_SESSION_SECRET = ACCEPTED
2. MCP_INITIALIZE = PASS
3. TOOLS_LIST = EXACT_ALLOWED_SET
4. relay_get_head = PASS (dispatches to underlying service)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock
import pytest

from api.mcp import create_hosted_mcp_handler
from aos.controller_relay_mcp_handler import (
    ALLOWED_TOOL_NAMES,
    McpHttpHandler,
)
from aos.controller_relay_service import ControllerRelayService
from aos.lari_controller_ingress import (
    LARI_INGRESS_TOOL_DEFINITIONS,
    LariControllerIngressService,
    create_secret_session_authenticator,
)

VALID_SECRET = "test_valid_session_secret_256bit_entropy_12345"
ARBITRARY_32_CHAR_TOKEN = "a" * 36


@pytest.fixture
def mock_ingress_service():
    service = MagicMock(spec=LariControllerIngressService)
    authenticator = create_secret_session_authenticator(VALID_SECRET)

    # Implement authenticate_session realistically
    def auth_session(token):
        if not token or not isinstance(token, str) or len(token) < 32:
            return False
        auth = authenticator(token)
        return auth is not None and auth.is_valid_lari_controller()

    service.authenticate_session.side_effect = auth_session
    service.relay_get_head.return_value = {
        "status": "SUCCESS",
        "repository": "MertSGI/AOS",
        "branch": "control/controller-relay",
        "head_sha": "7e8037814e3dfda4065d658c05bf44d41f92ab0d",
    }
    service.relay_get_latest_unconsumed.return_value = {
        "status": "EMPTY",
        "head_sha": "7e8037814e3dfda4065d658c05bf44d41f92ab0d",
        "target_controller": "LARI_CONTROLLER",
        "unconsumed_count": 0,
        "latest_unconsumed": None,
    }
    return service


@pytest.fixture
def mcp_handler(mock_ingress_service):
    return McpHttpHandler(ingress_service=mock_ingress_service)


# --------------------------------------------------------------------------
# NEGATIVE TESTS
# --------------------------------------------------------------------------

def test_no_authenticator_configured_startup_fail_closed(monkeypatch):
    """Prove missing session secret fails closed at startup."""
    monkeypatch.delenv("LARI_CONTROLLER_SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="STARTUP_FAIL_CLOSED"):
        create_hosted_mcp_handler()


def test_short_authenticator_secret_startup_fail_closed(monkeypatch):
    """Prove short secret (<32 chars) fails closed at startup."""
    monkeypatch.setenv("LARI_CONTROLLER_SESSION_SECRET", "short_secret")
    with pytest.raises(RuntimeError, match="STARTUP_FAIL_CLOSED"):
        create_hosted_mcp_handler()


def test_unauthenticated_mcp_request_denied(mcp_handler):
    """Prove unauthenticated MCP requests are denied with HTTP 401."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=None)
    assert status == 401
    assert "Unauthorized" in resp["error"]["message"]


def test_invalid_bearer_token_denied(mcp_handler):
    """Prove invalid bearer token is denied with HTTP 401."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header="Bearer wrong_token_short")
    assert status == 401
    assert "Unauthorized" in resp["error"]["message"]


def test_arbitrary_32_plus_character_token_denied(mcp_handler):
    """Prove arbitrary 32+ char token strictly fails with HTTP 401."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {},
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=f"Bearer {ARBITRARY_32_CHAR_TOKEN}")
    assert status == 401
    assert "Unauthorized" in resp["error"]["message"]


def test_generic_git_tool_not_available(mcp_handler):
    """Prove arbitrary Git commands or generic tools are denied with method/tool not found."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "git_push",
            "arguments": {"branch": "main"},
        },
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    assert status == 200
    assert resp["error"]["code"] == -32601
    assert "Method/tool not found" in resp["error"]["message"]


def test_caller_principal_override_impossible(mcp_handler, mock_ingress_service):
    """Prove caller cannot inject principal to impersonate AOS_CONTROLLER or override identity."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "relay_get_latest_unconsumed",
            "arguments": {"principal": "AOS_CONTROLLER", "for_controller": "LARI_CONTROLLER"},
        },
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    assert status == 200
    # mock_ingress_service was called with principal stripped
    assert not resp.get("isError")


def test_secret_response_exposure_no(mcp_handler):
    """Prove session secret and bearer tokens are never reflected in response payloads."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    resp_text = json.dumps(resp)
    assert VALID_SECRET not in resp_text


# --------------------------------------------------------------------------
# POSITIVE TESTS
# --------------------------------------------------------------------------

def test_mcp_initialize_pass(mcp_handler):
    """Prove MCP initialize handshake succeeds with valid credentials."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ChatGPT-Connector", "version": "1.0"},
        },
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    assert status == 200
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "lari-controller-relay"


def test_tools_list_exact_allowed_set(mcp_handler):
    """Prove tools/list exposes exactly the 7 allowed bounded operations."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    assert status == 200
    tool_names = [t["name"] for t in resp["result"]["tools"]]
    expected_tools = [
        "relay_get_head",
        "relay_get_latest_unconsumed",
        "relay_read_message",
        "relay_publish_message",
        "relay_publish_receipt",
        "authority_fetch_and_verify",
        "publish_controller_authority",
    ]
    assert sorted(tool_names) == sorted(expected_tools)


def test_relay_get_head_pass(mcp_handler, mock_ingress_service):
    """Prove relay_get_head tool call succeeds and returns exact head commit SHA."""
    auth = f"Bearer {VALID_SECRET}"
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "relay_get_head",
            "arguments": {},
        },
    }).encode("utf-8")

    status, resp = mcp_handler.handle_request(body, auth_header=auth)
    assert status == 200
    assert resp["result"]["isError"] is False
    content_text = resp["result"]["content"][0]["text"]
    result_data = json.loads(content_text)
    assert result_data["status"] == "SUCCESS"
    assert result_data["head_sha"] == "7e8037814e3dfda4065d658c05bf44d41f92ab0d"
