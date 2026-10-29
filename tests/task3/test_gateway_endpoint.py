import json

import httpx
import pytest

from mocks.llm_provider import create_mock_provider
from stream_guardrail.app import Settings, create_app
from stream_guardrail.sse import SSEParser

SECRET_TEXT = (
    "Sure. The customer's email is priya.raman@example.com, SSN 123-45-6789 "
    "and card 4111 1111 1111 1111."
)
REDACTED_TEXT = "Sure. The customer's email is [REDACTED], SSN [REDACTED] and card [REDACTED]."


def gateway(provider=None, **settings):
    provider = provider or create_mock_provider()
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=provider))
    app = create_app(Settings(upstream_base_url="http://provider/v1", **settings), upstream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


def request(**extra):
    return {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hi"}],
        "mock_response": SECRET_TEXT,
        **extra,
    }


def streamed_text(body: bytes) -> str:
    parser = SSEParser()
    text = ""
    for event in parser.feed(body) + parser.close():
        if event.data != "[DONE]":
            for choice in json.loads(event.data)["choices"]:
                text += choice["delta"].get("content") or ""
    return text


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 64])
async def test_streamed_response_is_redacted(chunk_size):
    async with gateway() as client:
        response = await client.post(
            "/v1/chat/completions", json=request(stream=True, mock_chunk_size=chunk_size)
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert streamed_text(response.content) == REDACTED_TEXT
    for secret in ("priya.raman", "123-45-6789", "4111"):
        assert secret not in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


async def test_non_streamed_response_is_redacted():
    async with gateway() as client:
        response = await client.post("/v1/chat/completions", json=request())
    assert response.json()["choices"][0]["message"]["content"] == REDACTED_TEXT


async def test_upstream_errors_are_sanitized():
    async with gateway() as client:
        response = await client.post("/v1/chat/completions", json=request(mock_status=500))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "Traceback" not in response.text


async def test_upstream_rate_limit_is_reported_as_429():
    async with gateway() as client:
        response = await client.post("/v1/chat/completions", json=request(mock_status=429))
    assert response.status_code == 429


async def test_unreachable_upstream():
    def refuse(request):
        raise httpx.ConnectError("connection refused 10.1.2.3:443")

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    app = create_app(Settings(upstream_base_url="http://provider/v1"), upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        response = await c.post("/v1/chat/completions", json=request(stream=True))
    assert response.status_code == 502
    assert "10.1.2.3" not in response.text


async def test_invalid_json_body():
    async with gateway() as client:
        response = await client.post("/v1/chat/completions", content=b"{nope")
    assert response.status_code == 400


async def test_provider_key_is_sent_upstream_and_client_key_is_not():
    seen = {}

    async def capture(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
        return httpx.Response(200, json=body)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    app = create_app(
        Settings(upstream_base_url="http://provider/v1", upstream_api_key="sk-provider"), upstream
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        await c.post(
            "/v1/chat/completions", json=request(), headers={"authorization": "Bearer client-key"}
        )
    assert seen["authorization"] == "Bearer sk-provider"
