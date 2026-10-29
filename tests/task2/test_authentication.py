import pytest

from mcp_gateway.config import parse_tokens
from mocks.mcp_downstream import tool_calls

from .conftest import ADMIN_TOKEN, call, rpc


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": ""},
        {"authorization": "Bearer"},
        {"authorization": "Bearer    "},
        {"authorization": f"Basic {ADMIN_TOKEN}"},
        {"authorization": "Bearer not-a-real-token-at-all"},
        {"authorization": f"Bearer {ADMIN_TOKEN}x"},
    ],
)
async def test_requests_without_valid_bearer_token_are_rejected(gateway, downstream, headers):
    response = await gateway.post("/mcp", json=call("get_customer_record"), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.json()["error"]["code"] == -32002
    assert downstream.state.received == []


async def test_bearer_scheme_is_case_insensitive(gateway):
    response = await gateway.post(
        "/mcp", json=rpc("tools/list"), headers={"authorization": f"bearer {ADMIN_TOKEN}"}
    )
    assert response.status_code == 200


async def test_get_and_delete_also_require_auth(gateway):
    assert (await gateway.get("/mcp")).status_code == 401
    assert (await gateway.delete("/mcp")).status_code == 401


async def test_auth_failure_does_not_reach_downstream(gateway, downstream):
    await gateway.post("/mcp", json=call("admin_reset_key"))
    assert tool_calls(downstream) == []


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"short": {"subject": "a", "role": "admin"}},
        {"x" * 20: {"subject": "a", "role": "superuser"}},
        {"x" * 20: {"role": "admin"}},
        [],
    ],
)
def test_invalid_token_config_fails_at_startup(config):
    with pytest.raises(ValueError):
        parse_tokens(config)
