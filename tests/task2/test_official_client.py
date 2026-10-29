"""An agent using the official MCP client, connected through the gateway."""

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from mocks.mcp_downstream import create_mock_downstream, tool_calls

from .conftest import VIEWER_TOKEN, build_gateway


async def test_mcp_client_sees_unauthorized_tool_call_error():
    downstream = create_mock_downstream()
    gateway = build_gateway(downstream)

    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway),
        headers={"Authorization": f"Bearer {VIEWER_TOKEN}"},
    )
    async with (
        http_client,
        streamable_http_client("http://gateway/mcp", http_client=http_client) as (read, write, _),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"get_customer_record", "admin_reset_key"}

            ok = await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
            assert ok.isError is False

            with pytest.raises(McpError) as exc:
                await session.call_tool("admin_reset_key", {"tenant": "acme"})
            assert exc.value.error.code == -32001
            assert exc.value.error.message == "Unauthorized Tool Call"

    assert [c["params"]["name"] for c in tool_calls(downstream)] == ["get_customer_record"]
