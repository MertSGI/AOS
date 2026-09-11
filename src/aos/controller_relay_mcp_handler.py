"""Streamable HTTP MCP JSON-RPC Protocol Handler for LARI Controller Relay.

Implementation Authority: LARI-AOS-CR2-LITE-DEPLOYABLE-HOST-PACKAGING-20260911-01
Program ID: LARI-PROGRAM-V2-REAL-PRODUCT-20260908-01

Standards:
- Model Context Protocol (MCP) Specification (JSON-RPC 2.0).
- Streamable HTTP Transport: handles POST requests to /api/mcp with JSON-RPC payloads.
- Methods supported:
  * initialize
  * tools/list
  * tools/call
- Strict authentication: verifies Bearer token against LARI_CONTROLLER_SESSION_SECRET
  before any dispatch.
- Zero generic tools, zero caller principal overrides.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from aos.lari_controller_ingress import (
    LARI_INGRESS_TOOL_DEFINITIONS,
    LariControllerIngressService,
    dispatch_lari_ingress_call,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "lari-controller-relay"
SERVER_VERSION = "1.0.0"

# Exact allowed tool names
ALLOWED_TOOL_NAMES = {t["name"] for t in LARI_INGRESS_TOOL_DEFINITIONS}


class McpJsonRpcError(Exception):
    """JSON-RPC 2.0 Error representation."""

    def __init__(self, code: int, message: str, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


class McpHttpHandler:
    """Streamable HTTP MCP Handler processing JSON-RPC 2.0 requests."""

    def __init__(self, ingress_service: LariControllerIngressService):
        self._ingress_service = ingress_service

    def handle_request(
        self,
        raw_body: bytes,
        auth_header: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Process an inbound HTTP request to /api/mcp.

        Returns (http_status_code, json_response_dict).
        """
        # 1. Parse JSON body
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            return 400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }

        if not isinstance(payload, dict):
            return 400, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: root payload must be an object"},
            }

        req_id = payload.get("id")
        method = payload.get("method")
        params = payload.get("params", {})

        if not isinstance(method, str):
            return 400, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: missing or invalid method"},
            }

        # 2. Extract Bearer token
        session_token = self._extract_bearer_token(auth_header)

        # 3. Authentication check:
        # Per fail-closed invariant, unauthenticated requests are denied immediately.
        # initialize, tools/list, tools/call all require valid LARI_CONTROLLER session credentials.
        if not session_token or not self._ingress_service.authenticate_session(session_token):
            return 401, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": "Unauthorized: invalid, missing, or unauthorized session credential for LARI_CONTROLLER",
                },
            }

        # 4. Route method
        try:
            result = self._dispatch_method(method, params, session_token)
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except McpJsonRpcError as err:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": err.to_dict(),
            }
        except Exception as exc:
            return 500, {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal server error: {exc}"},
            }

    @staticmethod
    def _extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
        if not auth_header or not isinstance(auth_header, str):
            return None
        parts = auth_header.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        return None

    def _dispatch_method(
        self,
        method: str,
        params: Dict[str, Any],
        session_token: str,
    ) -> Dict[str, Any]:
        """Dispatch MCP JSON-RPC method."""
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            }

        elif method == "notifications/initialized":
            return {}

        elif method == "ping":
            return {}

        elif method == "tools/list":
            # Map canonical tool definitions to MCP format
            mcp_tools = []
            for tool in LARI_INGRESS_TOOL_DEFINITIONS:
                mcp_tools.append({
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": tool["input_schema"],
                })
            return {"tools": mcp_tools}

        elif method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments", {})

            if not tool_name or tool_name not in ALLOWED_TOOL_NAMES:
                raise McpJsonRpcError(
                    -32601,
                    f"Method/tool not found: '{tool_name}'. Allowed tools: {sorted(list(ALLOWED_TOOL_NAMES))}",
                )

            # Security: Caller principal cannot be overridden
            # Strip any caller attempts to inject principal
            if isinstance(arguments, dict):
                arguments = {k: v for k, v in arguments.items() if k not in ("principal", "caller_principal", "controller_id")}

            raw_result = dispatch_lari_ingress_call(
                service=self._ingress_service,
                tool_name=tool_name,
                arguments=arguments,
                session_token=session_token,
            )

            # Format as MCP call tool result (array of content blocks)
            # If status == 'ERROR' or 'REJECTED' or 'UNAUTHORIZED', mark isError = True
            is_error = raw_result.get("status") in ("ERROR", "REJECTED", "UNAUTHORIZED")
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(raw_result, indent=2, sort_keys=True),
                    }
                ],
                "isError": is_error,
            }

        else:
            raise McpJsonRpcError(-32601, f"Method not found: '{method}'")
