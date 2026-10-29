"""Walk through all four tasks in one process, no servers or Docker needed.

Run: uv run python scripts/demo.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from mcp_gateway.app import create_app as create_mcp_gateway
from mcp_gateway.config import Settings as GatewaySettings
from mcp_gateway.config import parse_tokens
from mocks.llm_provider import create_mock_provider
from mocks.mcp_downstream import create_mock_downstream, tool_calls
from model_router.app import Settings as RouterSettings
from model_router.app import create_app as create_router
from model_router.limiter import SlidingWindowLimiter
from model_router.router import Provider
from stream_guardrail.app import Settings as GuardrailSettings
from stream_guardrail.app import create_app as create_guardrail


def heading(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(label: str, value: object) -> None:
    text = value if isinstance(value, str) else json.dumps(value)
    print(f"  {label:<38} {text}")


def asgi_client(app, base_url: str = "http://local") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


def task1_mcp_server() -> None:
    heading("Task 1  MCP server over stdio")
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "demo", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_customer_record", "arguments": {"customer_id": "CUST-10001"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_customer_record", "arguments": {"customer_id": "cust-1"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "trigger_refund",
                "arguments": {"customer_id": "CUST-10001", "amount": True, "reason": "short"},
            },
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "customer_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("pipes were not created")
    replies, raw_lines = {}, []
    for message in messages:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
        if "id" in message:
            raw_lines.append(proc.stdout.readline())
            reply = json.loads(raw_lines[-1])
            replies[reply["id"]] = reply
    rest, stderr_bytes = proc.communicate(timeout=10)
    raw_lines += rest.splitlines()
    stderr = stderr_bytes.decode()

    def is_jsonrpc(line: bytes) -> bool:
        try:
            return json.loads(line).get("jsonrpc") == "2.0"
        except ValueError:
            return False

    show("valid lookup -> isError", replies[2]["result"]["isError"])
    show("customer_id 'cust-1' -> error code", replies[3]["error"]["code"])
    fields = [e["field"] for e in replies[4]["error"]["data"]["errors"]]
    show("amount=True, short reason -> fields", fields)
    show("stdout lines that were not JSON-RPC", sum(not is_jsonrpc(x) for x in raw_lines))
    show("log lines on stderr", len(stderr.splitlines()))


async def task2_mcp_gateway() -> None:
    heading("Task 2  MCP security gateway")
    downstream = create_mock_downstream()
    settings = GatewaySettings(
        downstream_url="http://downstream/mcp",
        tokens=parse_tokens(
            {
                "admin-token-0123456789abcdef": {"subject": "alice", "role": "admin"},
                "viewer-token-0123456789abcdef": {"subject": "bob", "role": "viewer"},
            }
        ),
    )
    app = create_mcp_gateway(settings, downstream=asgi_client(downstream, "http://downstream"))

    async with asgi_client(app) as gw:

        async def call(token: str, name: str):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}
            r = await gw.post("/mcp", json=body, headers={"authorization": f"Bearer {token}"})
            return r.json()

        viewer, admin = "viewer-token-0123456789abcdef", "admin-token-0123456789abcdef"
        show("viewer -> admin_reset_key", (await call(viewer, "admin_reset_key"))["error"])
        show("viewer -> ADMIN_reset_key", (await call(viewer, "ADMIN_reset_key"))["error"]["code"])
        show("viewer -> get_customer_record", "result" in await call(viewer, "get_customer_record"))
        show("admin  -> admin_reset_key", "result" in await call(admin, "admin_reset_key"))
        r = await gw.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        show("no token -> HTTP status", r.status_code)
    show(
        "tool calls that reached downstream", [c["params"]["name"] for c in tool_calls(downstream)]
    )


async def task3_guardrail() -> None:
    heading("Task 3  Streaming PII guardrail")
    text = "Reach Priya at priya.raman@example.com, SSN 123-45-6789, card 4111 1111 1111 1111."
    provider = asgi_client(create_mock_provider(), "http://provider")
    app = create_guardrail(GuardrailSettings(upstream_base_url="http://provider/v1"), provider)
    async with asgi_client(app) as gw:
        r = await gw.post(
            "/v1/chat/completions",
            json={
                "model": "demo",
                "stream": True,
                "mock_chunk_size": 3,
                "mock_response": text,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    deltas = [
        c["delta"].get("content") or ""
        for line in r.text.splitlines()
        if line.startswith("data: {")
        for c in json.loads(line[6:])["choices"]
    ]
    show("provider sent (3 chars per chunk)", text)
    show("client received", "".join(deltas))
    show("chunks forwarded", len([d for d in deltas if d]))


async def task4_router() -> None:
    heading("Task 4  Rate limiter + model failover")
    primary_rate_limited = create_mock_provider("primary", fail_status=429)
    primary_slow = create_mock_provider("primary", first_byte_delay_ms=4_000)
    secondary = create_mock_provider("secondary")

    async def run(primary, label: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            limiter = SlidingWindowLimiter(Path(tmp) / "router.db", 50_000)
            transport = _Hosts({"primary": primary, "secondary": secondary})
            settings = RouterSettings(
                tenant_keys=RouterSettings.hash_keys({"sk-tenant-alpha-0123456789": "alpha"}),
                providers=(
                    Provider("primary", "http://primary/v1"),
                    Provider("secondary", "http://secondary/v1"),
                ),
            )
            app = create_router(
                settings, client=httpx.AsyncClient(transport=transport), limiter=limiter
            )
            async with asgi_client(app) as gw:
                started = time.perf_counter()
                r = await gw.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
                    headers={"authorization": "Bearer sk-tenant-alpha-0123456789"},
                )
                elapsed = time.perf_counter() - started
                show(
                    label,
                    f"served by {r.headers['x-gateway-provider']} "
                    f"after {r.headers['x-gateway-attempts']} attempts in {elapsed:.2f}s",
                )
                if label.startswith("primary 429"):
                    results = await asyncio.gather(
                        *(limiter.reserve("beta", 2_000) for _ in range(50)), return_exceptions=True
                    )
                    granted = sum(not isinstance(x, Exception) for x in results)
                    show("50 concurrent x 2,000 tokens, 50k/min", f"{granted} granted")
                    r = await gw.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 60_000,
                        },
                        headers={"authorization": "Bearer sk-tenant-alpha-0123456789"},
                    )
                    show("request over the limit", r.json()["error"])
            await limiter.close()

    await run(primary_rate_limited, "primary 429 ->")
    await run(primary_slow, "primary slow (4s, timeout 3s) ->")


class _Hosts(httpx.AsyncBaseTransport):
    def __init__(self, apps) -> None:
        self._apps = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._apps[request.url.host].handle_async_request(request)


async def main() -> None:
    task1_mcp_server()
    await task2_mcp_gateway()
    await task3_guardrail()
    await task4_router()
    print()


if __name__ == "__main__":
    asyncio.run(main())
