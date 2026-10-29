"""A small downstream MCP server (Streamable HTTP, JSON or SSE responses) for the gateway.

It records every request it receives, which lets the tests prove that a
blocked call never reached it.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Look up a customer record.",
        "inputSchema": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Rotate a tenant's API key.",
        "inputSchema": {"type": "object", "properties": {"tenant": {"type": "string"}}},
    },
]


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    if "id" not in message:
        return None  # notification
    rpc_id = message["id"]
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mock-downstream", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = message.get("params", {}).get("name")
        result = {
            "content": [{"type": "text", "text": f"downstream executed {name}"}],
            "isError": False,
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def create_mock_downstream(sse: bool = False) -> Starlette:
    async def mcp(request: Request) -> Response:
        body = await request.json()
        request.app.state.received.append({"body": body, "headers": dict(request.headers)})
        if isinstance(body, list):
            replies: Any = [r for r in (_handle(m) for m in body) if r is not None]
        else:
            replies = _handle(body)
        if not replies:
            return Response(status_code=202)
        headers = {"mcp-session-id": request.headers.get("mcp-session-id") or uuid.uuid4().hex}
        if sse:
            events = f"event: message\ndata: {json.dumps(replies)}\n\n"
            return Response(events, media_type="text/event-stream", headers=headers)
        return JSONResponse(replies, headers=headers)

    async def not_allowed(_: Request) -> Response:
        return Response(status_code=405)

    async def calls(request: Request) -> Response:
        return JSONResponse(tool_calls(request.app))

    app = Starlette(
        routes=[
            Route("/mcp", mcp, methods=["POST"]),
            Route("/mcp", not_allowed, methods=["GET", "DELETE"]),
            Route("/calls", calls, methods=["GET"]),
        ]
    )
    app.state.received = []
    return app


def tool_calls(app: Starlette) -> list[dict[str, Any]]:
    """Every tools/call the mock received, including ones inside batches."""
    found = []
    for entry in app.state.received:
        bodies = entry["body"] if isinstance(entry["body"], list) else [entry["body"]]
        found += [b for b in bodies if isinstance(b, dict) and b.get("method") == "tools/call"]
    return found


def main() -> None:
    import uvicorn

    app = create_mock_downstream(sse=os.environ.get("MOCK_SSE") == "1")
    uvicorn.run(
        app,
        host=os.environ.get("MOCK_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_PORT", "9000")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
