"""Round trip through the official MCP Python client, the way a real agent would connect."""

import os
import sys
import tempfile

import mcp.types as types
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


async def test_official_client_round_trip():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "customer_mcp"], env=dict(os.environ)
    )
    with tempfile.TemporaryFile("w+") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"get_customer_record", "trigger_refund"}

                ok = await session.call_tool("get_customer_record", {"customer_id": "CUST-10002"})
                assert ok.isError is False

                with pytest.raises(McpError) as exc:
                    await session.call_tool("get_customer_record", {"customer_id": "nope"})
                assert exc.value.error.code == types.INVALID_PARAMS
