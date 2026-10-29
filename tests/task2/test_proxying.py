import httpx
import pytest

from mcp_gateway.app import create_app
from mcp_gateway.config import Settings, parse_tokens
from mocks.mcp_downstream import create_mock_downstream

from .conftest import ADMIN_TOKEN, TOKENS, VIEWER_TOKEN, auth, build_gateway, call, rpc


async def test_client_credentials_and_spoofed_identity_never_reach_downstream(gateway, downstream):
    headers = {
        **auth(VIEWER_TOKEN),
        "x-gateway-role": "admin",
        "x-gateway-subject": "root",
        "cookie": "session=abc",
    }
    await gateway.post("/mcp", json=call("get_customer_record"), headers=headers)
    forwarded = downstream.state.received[0]["headers"]
    assert "authorization" not in forwarded
    assert "cookie" not in forwarded
    assert forwarded["x-gateway-role"] == "viewer"
    assert forwarded["x-gateway-subject"] == "bob@example.com"
    assert forwarded["x-request-id"]


async def test_session_header_round_trips(gateway, downstream):
    response = await gateway.post(
        "/mcp", json=rpc("initialize", {}), headers={**auth(VIEWER_TOKEN), "mcp-session-id": "s-1"}
    )
    assert downstream.state.received[0]["headers"]["mcp-session-id"] == "s-1"
    assert response.headers["mcp-session-id"] == "s-1"
    assert response.headers["x-request-id"]


async def test_original_bytes_are_forwarded_unchanged(gateway, downstream):
    raw = b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"get_customer_record"}}'
    await gateway.post(
        "/mcp", content=raw, headers={**auth(VIEWER_TOKEN), "content-type": "application/json"}
    )
    assert downstream.state.received[0]["body"]["id"] == 9


async def test_sse_responses_are_streamed_through():
    downstream = create_mock_downstream(sse=True)
    app = build_gateway(downstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as c:
        response = await c.post("/mcp", json=call("get_customer_record"), headers=auth(ADMIN_TOKEN))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.startswith("event: message\ndata: ")


async def test_downstream_outage_returns_a_clean_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 10.0.0.12:9000")

    settings = Settings(downstream_url="http://downstream/mcp", tokens=parse_tokens(TOKENS))
    downstream = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    app = create_app(settings, downstream=downstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as c:
        response = await c.post(
            "/mcp", json=call("get_customer_record", 5), headers=auth(ADMIN_TOKEN)
        )
    assert response.status_code == 502
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32003, "message": "Downstream MCP server unavailable"},
    }
    assert "10.0.0.12" not in response.text


@pytest.mark.parametrize("sse", [False, True])
async def test_optional_discovery_filtering_hides_admin_tools_from_viewers(sse):
    downstream = create_mock_downstream(sse=sse)
    app = build_gateway(downstream, hide_unauthorized_tools=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g") as c:
        viewer = await c.post("/mcp", json=rpc("tools/list"), headers=auth(VIEWER_TOKEN))
        admin = await c.post("/mcp", json=rpc("tools/list"), headers=auth(ADMIN_TOKEN))

    def names(response: httpx.Response) -> set[str]:
        import json

        text = response.text
        payload = json.loads(text.split("data: ", 1)[1]) if sse else response.json()
        return {t["name"] for t in payload["result"]["tools"]}

    assert names(viewer) == {"get_customer_record"}
    assert names(admin) == {"get_customer_record", "admin_reset_key"}


async def test_healthz(gateway):
    assert (await gateway.get("/healthz")).json() == {"status": "ok"}
