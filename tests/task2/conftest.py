from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings, parse_tokens
from mocks.mcp_downstream import create_mock_downstream

ADMIN_TOKEN = "admin-token-0123456789abcdef"
VIEWER_TOKEN = "viewer-token-0123456789abcdef"
TOKENS = {
    ADMIN_TOKEN: {"subject": "alice@example.com", "role": "admin"},
    VIEWER_TOKEN: {"subject": "bob@example.com", "role": "viewer"},
}


def build_gateway(downstream: Starlette, **overrides: Any) -> Starlette:
    settings = Settings(
        downstream_url="http://downstream/mcp", tokens=parse_tokens(TOKENS), **overrides
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=downstream), base_url="http://downstream"
    )
    return create_app(settings, downstream=client)


@pytest.fixture
def downstream() -> Starlette:
    return create_mock_downstream()


@pytest.fixture
async def gateway(downstream: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    app = build_gateway(downstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield client


def rpc(method: str, params: Any = None, rpc_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(name: str, rpc_id: Any = 1) -> dict[str, Any]:
    return rpc("tools/call", {"name": name, "arguments": {}}, rpc_id)


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}
