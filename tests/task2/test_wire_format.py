import json

import pytest

from .conftest import VIEWER_TOKEN, auth, call

HEADERS = {**auth(VIEWER_TOKEN), "content-type": "application/json"}


async def post_raw(gateway, body: bytes | str):
    return await gateway.post("/mcp", content=body, headers=HEADERS)


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "",
        '{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": NaN}',
        b"\xff\xfe\x00",
    ],
)
async def test_unparseable_bodies_are_parse_errors(gateway, downstream, body):
    response = await post_raw(gateway, body)
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert downstream.state.received == []


async def test_duplicate_keys_are_rejected_to_prevent_parser_differentials(gateway, downstream):
    # Python keeps the last "name", other parsers keep the first. The gateway
    # must not authorize one tool while the downstream runs another.
    smuggled = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"get_customer_record","name":"admin_reset_key"}}'
    )
    response = await post_raw(gateway, smuggled)
    assert response.json()["error"]["code"] == -32700
    assert downstream.state.received == []


@pytest.mark.parametrize(
    "message",
    [
        42,
        "tools/call",
        None,
        {"id": 1, "method": "tools/list"},  # missing jsonrpc
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1},  # no method
        {"jsonrpc": "2.0", "id": 1, "method": 7},
        {"jsonrpc": "2.0", "id": 1, "method": ""},
        {"jsonrpc": "2.0", "id": True, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": {"a": 1}, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "admin_reset_key"},
    ],
)
async def test_invalid_requests(gateway, downstream, message):
    response = await post_raw(gateway, json.dumps(message))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    assert downstream.state.received == []


async def test_invalid_request_keeps_the_id_when_it_is_usable(gateway):
    response = await post_raw(gateway, json.dumps({"jsonrpc": "1.0", "id": "abc", "method": "x"}))
    assert response.json()["id"] == "abc"


@pytest.mark.parametrize(
    "params",
    [None, {}, {"name": None}, {"name": 5}, {"name": ""}, {"name": ["admin_reset_key"]}],
)
async def test_tools_call_without_a_valid_name_is_invalid_params(gateway, downstream, params):
    message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
    if params is not None:
        message["params"] = params
    response = await post_raw(gateway, json.dumps(message))
    assert response.json()["error"]["code"] == -32602
    assert response.json()["id"] == 3
    assert downstream.state.received == []


async def test_oversized_body_is_rejected(downstream):
    import httpx

    from .conftest import build_gateway

    app = build_gateway(downstream, max_body_bytes=200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        big = call("get_customer_record")
        big["params"]["arguments"] = {"padding": "x" * 500}
        response = await client.post("/mcp", json=big, headers=auth(VIEWER_TOKEN))
    assert response.status_code == 413
    assert downstream.state.received == []
