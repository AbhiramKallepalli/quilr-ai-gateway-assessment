import asyncio

import httpx
import pytest

from mocks.llm_provider import create_mock_provider
from model_router.app import Settings, create_app
from model_router.limiter import SlidingWindowLimiter
from model_router.router import Provider

from .conftest import OTHER_TENANT_KEY, TENANT_KEY

LEAKS = (
    "Traceback",
    "simulated failure",
    "primary-model",
    "secondary-model",
    "http://",
    "sk-primary-secret",
)


def assert_standard_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    error = response.json()["error"]
    assert set(error) == {"type", "code", "message", "request_id"}
    assert error["code"] == code
    assert error["request_id"] == response.headers["x-request-id"]
    for leak in LEAKS:
        assert leak not in response.text


@pytest.mark.parametrize("key", [None, "", "sk-unknown", f"Basic {TENANT_KEY}"])
async def test_requires_a_tenant_key(make_gateway, key):
    gw = await make_gateway()
    headers = {"authorization": key if key and key.startswith("Basic") else f"Bearer {key}"}
    if key is None:
        headers = {}
    response = await gw.client.post("/v1/chat/completions", json={"messages": []}, headers=headers)
    assert_standard_error(response, 401, "invalid_api_key")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": "hi"},
        {"messages": [{"content": "no role"}]},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": -1},
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": True},
    ],
)
async def test_invalid_bodies(make_gateway, body):
    gw = await make_gateway()
    response = await gw.client.post(
        "/v1/chat/completions", json=body, headers={"authorization": f"Bearer {TENANT_KEY}"}
    )
    assert_standard_error(response, 400, "invalid_request")


async def test_tenant_over_limit_gets_429_with_retry_after(make_gateway, clock):
    gw = await make_gateway(limit=1_000)
    await gw.limiter.reserve("alpha", 950)  # usage from earlier requests
    clock.advance(30)
    response = await gw.complete(max_tokens=900)
    assert_standard_error(response, 429, "tenant_rate_limited")
    assert response.headers["retry-after"] == "30"
    assert response.headers["x-ratelimit-remaining-tokens"] == "50"
    assert gw.primary.state.calls == 0  # rejected before any provider was called


async def test_limits_are_per_tenant(make_gateway):
    gw = await make_gateway(limit=1_000)
    assert (await gw.complete(max_tokens=900)).status_code == 200
    assert (await gw.complete(key=OTHER_TENANT_KEY, max_tokens=900)).status_code == 200


async def test_actual_usage_replaces_the_estimate(make_gateway):
    gw = await make_gateway()
    response = await gw.complete(max_tokens=4_000)
    reported = response.json()["usage"]["total_tokens"]
    assert await gw.limiter.usage("alpha") == reported < 4_000


async def test_failed_requests_are_not_charged(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", fail_status=500),
        secondary=create_mock_provider("s", fail_status=500),
    )
    response = await gw.complete(max_tokens=4_000)
    assert_standard_error(response, 502, "upstream_unavailable")
    assert await gw.limiter.usage("alpha") == 0


async def test_both_providers_rate_limited(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", fail_status=429),
        secondary=create_mock_provider("s", fail_status=429),
    )
    assert_standard_error(await gw.complete(), 429, "upstream_rate_limited")


async def test_both_providers_time_out(make_gateway):
    slow = dict(first_byte_delay_ms=1_000)
    gw = await make_gateway(
        primary=create_mock_provider("p", **slow),
        secondary=create_mock_provider("s", **slow),
        timeout_s=0.1,
    )
    assert_standard_error(await gw.complete(), 504, "upstream_timeout")


async def test_unexpected_errors_are_sanitized(tmp_path, clock):
    class Exploding(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise RuntimeError("secret internal detail at /srv/app/router.py line 42")

    settings = Settings(
        tenant_keys=Settings.hash_keys({TENANT_KEY: "alpha"}),
        providers=(Provider("primary", "http://primary/v1"),),
    )
    limiter = SlidingWindowLimiter(tmp_path / "r.db", 50_000, clock=clock)
    app = create_app(settings, client=httpx.AsyncClient(transport=Exploding()), limiter=limiter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        response = await c.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {TENANT_KEY}"},
        )
    await limiter.close()
    assert_standard_error(response, 500, "internal_error")
    assert "secret internal detail" not in response.text


async def test_concurrent_requests_respect_the_limit(make_gateway):
    # Each request reserves 2,004 tokens; the slow provider keeps every reservation
    # open until all of them have been decided.
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=150), limit=10_000, timeout_s=2.0
    )
    responses = await asyncio.gather(
        *(
            gw.complete(max_tokens=2_000, messages=[{"role": "user", "content": ""}])
            for _ in range(20)
        )
    )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 4
    assert statuses.count(429) == 16
