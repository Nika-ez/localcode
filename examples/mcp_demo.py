"""A tiny MCP stdio server used by LocalCode's automated tests.

Run directly:  python examples/mcp_demo.py
It speaks newline-delimited JSON-RPC 2.0 on stdin/stdout.
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back. Useful for testing MCP.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First number"},
                "b": {"type": "number", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "long_task",
        "description": "Simulate a long task and return a completion message.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "description": "Seconds to wait"}},
        },
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def handle(msg: dict) -> None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        send({
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "localcode-demo", "version": "1.0.0"},
            },
        })
    elif method == "notifications/initialized":
        log("demo server initialized")
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "echo":
                text = str(args.get("text") or "")
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": f"echo: {text}"}]
                }})
            elif name == "add":
                result = float(args.get("a") or 0) + float(args.get("b") or 0)
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": f"sum={result:g}"}],
                    "structuredContent": {"sum": result},
                }})
            elif name == "long_task":
                seconds = max(0.0, min(float(args.get("seconds") or 1), 10))
                time.sleep(seconds)
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": f"finished after {seconds:g}s"}]
                }})
            else:
                send({"jsonrpc": "2.0", "id": req_id, "error": {
                    "code": -32602, "message": f"Unknown tool: {name}"
                }})
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}],
            }})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {
            "code": -32601, "message": f"Method not found: {method}"
        }})


def main() -> int:
    log("localcode demo MCP server ready")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            log(f"invalid JSON: {line[:200]}")
            continue
        if isinstance(msg, dict):
            handle(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
