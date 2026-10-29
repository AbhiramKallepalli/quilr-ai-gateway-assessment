import asyncio
import time

import pytest

from mocks.llm_provider import create_mock_provider


async def test_primary_serves_when_healthy(make_gateway):
    gw = await make_gateway()
    response = await gw.complete()
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "primary"
    assert response.headers["x-gateway-attempts"] == "1"
    assert gw.secondary.state.calls == 0


async def test_primary_429_fails_over_to_secondary(make_gateway):
    gw = await make_gateway(primary=create_mock_provider("p", fail_status=429))
    response = await gw.complete()
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "secondary"
    assert response.headers["x-gateway-attempts"] == "2"
    assert response.json()["model"] == "backup-model"  # secondary's model override


async def test_primary_timeout_fails_over_to_secondary(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=2_000), timeout_s=0.2
    )
    started = time.perf_counter()
    response = await gw.complete()
    elapsed = time.perf_counter() - started
    assert response.headers["x-gateway-provider"] == "secondary"
    assert 0.2 <= elapsed < 1.0  # waited for the timeout, not for the slow primary


async def test_default_timeout_is_3000ms(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=3_600), timeout_s=3.0
    )
    started = time.perf_counter()
    response = await gw.complete()
    elapsed = time.perf_counter() - started
    assert response.headers["x-gateway-provider"] == "secondary"
    assert 3.0 <= elapsed < 3.5


@pytest.mark.parametrize(("delay_ms", "provider"), [(100, "primary"), (500, "secondary")])
async def test_response_either_beats_the_timeout_or_loses_it(make_gateway, delay_ms, provider):
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=delay_ms), timeout_s=0.3
    )
    response = await gw.complete()
    assert response.headers["x-gateway-provider"] == provider


async def test_timed_out_primary_is_cancelled_not_left_running(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=1_000), timeout_s=0.2
    )
    await gw.complete()
    await asyncio.sleep(1.2)  # long after the primary would have finished
    assert gw.primary.state.calls == 1
    assert gw.primary.state.completed == 0


async def test_server_errors_fail_over(make_gateway):
    gw = await make_gateway(primary=create_mock_provider("p", fail_status=503))
    response = await gw.complete()
    assert response.headers["x-gateway-provider"] == "secondary"


async def test_client_errors_do_not_fail_over(make_gateway):
    gw = await make_gateway(primary=create_mock_provider("p", fail_status=400))
    response = await gw.complete()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "upstream_rejected"
    assert gw.secondary.state.calls == 0


async def test_rate_limited_primary_is_skipped_while_cooling_down(make_gateway):
    gw = await make_gateway(primary=create_mock_provider("p", fail_status=429, retry_after_s=30))
    await gw.complete()
    await gw.complete()
    assert gw.primary.state.calls == 1  # second request went straight to the secondary
    assert gw.secondary.state.calls == 2


async def test_streaming_fails_over_before_the_first_chunk(make_gateway):
    gw = await make_gateway(
        primary=create_mock_provider("p", first_byte_delay_ms=2_000), timeout_s=0.2
    )
    response = await gw.complete(stream=True, mock_response="streamed answer")
    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "secondary"
    assert "data: [DONE]" in response.text
