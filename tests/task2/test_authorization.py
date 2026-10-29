import pytest

from mocks.mcp_downstream import tool_calls

from .conftest import ADMIN_TOKEN, VIEWER_TOKEN, auth, call, rpc

UNAUTHORIZED_TOOL_CALL = -32001


async def test_tools_list_is_forwarded_transparently_for_viewers(gateway, downstream):
    response = await gateway.post("/mcp", json=rpc("tools/list"), headers=auth(VIEWER_TOKEN))
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert names == {"get_customer_record", "admin_reset_key"}
    assert downstream.state.received[0]["body"]["method"] == "tools/list"


async def test_viewer_can_call_regular_tools(gateway, downstream):
    response = await gateway.post(
        "/mcp", json=call("get_customer_record"), headers=auth(VIEWER_TOKEN)
    )
    assert response.json()["result"]["isError"] is False
    assert len(tool_calls(downstream)) == 1


async def test_viewer_calling_admin_tool_is_blocked_before_downstream(gateway, downstream):
    response = await gateway.post(
        "/mcp", json=call("admin_reset_key", rpc_id="req-7"), headers=auth(VIEWER_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "jsonrpc": "2.0",
        "id": "req-7",
        "error": {
            "code": UNAUTHORIZED_TOOL_CALL,
            "message": "Unauthorized Tool Call",
            "data": {"tool": "admin_reset_key", "required_role": "admin"},
        },
    }
    assert downstream.state.received == []


async def test_admin_can_call_admin_tools(gateway, downstream):
    response = await gateway.post("/mcp", json=call("admin_reset_key"), headers=auth(ADMIN_TOKEN))
    assert response.json()["result"]["content"][0]["text"] == "downstream executed admin_reset_key"
    assert len(tool_calls(downstream)) == 1


@pytest.mark.parametrize(
    "name",
    [
        "ADMIN_reset_key",
        "Admin_Reset_Key",
        " admin_reset_key",
        "ａｄｍｉｎ_reset_key",  # full-width letters fold to "admin_" under NFKC
        "admin_",
    ],
)
async def test_admin_prefix_variants_are_still_blocked(gateway, downstream, name):
    response = await gateway.post("/mcp", json=call(name), headers=auth(VIEWER_TOKEN))
    assert response.json()["error"]["code"] == UNAUTHORIZED_TOOL_CALL
    assert downstream.state.received == []


@pytest.mark.parametrize(
    "name", ["ad\u200bmin_reset_key", "admin_reset_key\u200b", "\tadmin_reset_key", "admin\x00_x"]
)
async def test_tool_names_with_hidden_characters_are_rejected(gateway, downstream, name):
    response = await gateway.post("/mcp", json=call(name), headers=auth(ADMIN_TOKEN))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
    assert downstream.state.received == []


@pytest.mark.parametrize("name", ["administrator_report", "get_admin_status", "reset_admin"])
async def test_only_the_prefix_matters(gateway, name):
    response = await gateway.post("/mcp", json=call(name), headers=auth(VIEWER_TOKEN))
    assert "result" in response.json()


async def test_denied_notification_gets_no_body_and_is_not_forwarded(gateway, downstream):
    notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_x"}}
    response = await gateway.post("/mcp", json=notification, headers=auth(VIEWER_TOKEN))
    assert response.status_code == 202
    assert response.content == b""
    assert downstream.state.received == []


async def test_lifecycle_and_other_methods_pass_through(gateway, downstream):
    for method in ("initialize", "ping", "resources/list"):
        await gateway.post("/mcp", json=rpc(method, {}), headers=auth(VIEWER_TOKEN))
    assert [r["body"]["method"] for r in downstream.state.received] == [
        "initialize",
        "ping",
        "resources/list",
    ]
