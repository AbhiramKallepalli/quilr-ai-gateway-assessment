from mocks.mcp_downstream import tool_calls

from .conftest import ADMIN_TOKEN, VIEWER_TOKEN, auth, call, rpc


async def test_admin_call_hidden_in_a_batch_blocks_the_whole_batch(gateway, downstream):
    batch = [call("get_customer_record", 1), call("admin_reset_key", 2), rpc("ping", rpc_id=3)]
    response = await gateway.post("/mcp", json=batch, headers=auth(VIEWER_TOKEN))
    assert response.status_code == 200
    by_id = {r["id"]: r["error"]["code"] for r in response.json()}
    assert by_id == {1: -32004, 2: -32001, 3: -32004}
    assert downstream.state.received == []


async def test_fully_authorized_batch_is_forwarded(gateway, downstream):
    batch = [call("get_customer_record", 1), call("admin_reset_key", 2)]
    response = await gateway.post("/mcp", json=batch, headers=auth(ADMIN_TOKEN))
    assert [r["id"] for r in response.json()] == [1, 2]
    assert len(tool_calls(downstream)) == 2


async def test_invalid_member_rejects_the_batch(gateway, downstream):
    batch = [call("get_customer_record", 1), {"jsonrpc": "2.0", "id": 2}]
    response = await gateway.post("/mcp", json=batch, headers=auth(ADMIN_TOKEN))
    codes = {r["id"]: r["error"]["code"] for r in response.json()}
    assert codes == {1: -32004, 2: -32600}
    assert downstream.state.received == []


async def test_empty_batch_is_invalid(gateway):
    response = await gateway.post("/mcp", json=[], headers=auth(ADMIN_TOKEN))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


async def test_batch_of_denied_notifications_returns_no_body(gateway, downstream):
    notes = [{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_a"}}]
    response = await gateway.post("/mcp", json=notes, headers=auth(VIEWER_TOKEN))
    assert response.status_code == 202
    assert downstream.state.received == []
