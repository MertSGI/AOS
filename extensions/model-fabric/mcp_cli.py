"""Command-line entrypoint for standard stdio MCP communication."""

import json
import sys
from extensions.model_fabric.mcp_service import NemotronMcpServer


def main():
    server = NemotronMcpServer()

    # Bounded stdin/stdout JSON-RPC loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        msg_id = req.get("id")

        if method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": server.list_tools()},
            }
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            call_res = server.call_tool(name, arguments)
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": call_res,
            }
        elif method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aos-nemotron-specialist", "version": "1.0.0"},
                },
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {},
            }

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
