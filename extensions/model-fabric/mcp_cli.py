"""Command-line entrypoint for standard stdio MCP JSON-RPC 2.0 communication.

Per Controller Audit R2:
- Proper JSON-RPC 2.0 protocol handling
- Parse error: -32700
- Invalid request: -32600
- Method not found: -32601
- Invalid params: -32602
- Notifications without id: NO RESPONSE
- Request ID preservation
- stdout purity: JSON-RPC output only
- stderr: sanitized diagnostics only
- Clean EOF / process shutdown
"""

import json
import sys
from typing import Any, Dict, Optional
from extensions.model_fabric.mcp_service import NemotronMcpServer

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def send_response(resp: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()


def send_error(req_id: Optional[Any], code: int, message: str, data: Optional[Any] = None) -> None:
    err_obj: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err_obj["data"] = data
    send_response({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": err_obj,
    })


def main() -> None:
    server = NemotronMcpServer()

    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue

        try:
            req = json.loads(raw)
        except Exception:
            send_error(None, PARSE_ERROR, "Parse error: Invalid JSON")
            continue

        if not isinstance(req, dict):
            send_error(None, INVALID_REQUEST, "Invalid Request: expected JSON object")
            continue

        req_id = req.get("id")
        method = req.get("method")
        jsonrpc = req.get("jsonrpc")

        # JSON-RPC 2.0 version validation (fail-closed)
        if jsonrpc != "2.0":
            send_error(req_id, INVALID_REQUEST, "Invalid Request: 'jsonrpc' must be exactly '2.0'")
            continue

        # JSON-RPC validation
        if not isinstance(method, str):
            send_error(req_id, INVALID_REQUEST, "Invalid Request: missing or invalid method")
            continue

        # Invariant: Notifications (without id) MUST NOT receive a response
        is_notification = ("id" not in req)

        if method == "notifications/initialized" or method == "initialized":
            # Handled notification
            sys.stderr.write("MCP connection initialized\n")
            sys.stderr.flush()
            continue

        if is_notification:
            # Drop unknown notifications without error response
            continue

        # Request handling (with req_id)
        if method == "initialize":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aos-nemotron-specialist", "version": "1.0.0"},
                },
            })
        elif method == "tools/list":
            send_response({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": server.list_tools()},
            })
        elif method == "tools/call":
            params = req.get("params")
            if not isinstance(params, dict):
                send_error(req_id, INVALID_PARAMS, "Invalid params: expected object")
                continue

            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                send_error(req_id, INVALID_PARAMS, "Invalid params: 'name' must be string, 'arguments' must be object")
                continue

            try:
                call_res = server.call_tool(name, arguments)
                send_response({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": call_res,
                })
            except Exception as e:
                send_error(req_id, INTERNAL_ERROR, "Internal error during tool call")
        else:
            send_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")


if __name__ == "__main__":
    main()
