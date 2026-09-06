"""A harmless two-page legacy MCP fixture. tools/call always fails."""

import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    result = None
    if method == "initialize":
        if mode == "timeout":
            time.sleep(60)
        result = {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "doctor-test", "version": "1"},
            "instructions": "Use search before reading a record.",
        }
    elif method == "tools/list":
        cursor = message.get("params", {}).get("cursor")
        result = {
            "tools": [
                {
                    "name": "read" if cursor else "search",
                    "description": "Read one record." if cursor else "Search records.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
        if not cursor or mode == "cycle":
            result["nextCursor"] = "next"
    if result is not None:
        response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
    else:
        # tools/call cannot silently pass this fixture.
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "Unsupported"},
        }
    print(json.dumps(response), flush=True)
